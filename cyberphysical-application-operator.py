import kopf, requests, json, random, re
from kubernetes import client
from kubernetes.utils import create_from_dict

UPPER_FOLLOWED_BY_LOWER_RE = re.compile('(.)([A-Z][a-z]+)')
UPPER_FOLLOWED_BY_LOWER_RE = re.compile('(.)([A-Z][a-z]+)')
LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE = re.compile('([a-z0-9])([A-Z])')
LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE = re.compile('([a-z0-9])([A-Z])')


def delete_from_dict(k8s_client, data, verbose=False, namespace='default',
                     **kwargs):
    # If it is a list type, will need to iterate its items
    api_exceptions = []
    k8s_objects = []

    if "List" in data["kind"]:
        # Could be "List" or "Pod/Service/...List"
        # This is a list type. iterate within its items
        kind = data["kind"].replace("List", "")
        for yml_object in data["items"]:
            # Mitigate cases when server returns a xxxList object
            # See kubernetes-client/python#586
            if kind != "":
                yml_object["apiVersion"] = data["apiVersion"]
                yml_object["kind"] = kind
            try:
                deleted = delete_from_yaml_single_item(
                    k8s_client, yml_object, verbose, namespace=namespace,
                    **kwargs)
                k8s_objects.append(deleted)
            except client.rest.ApiException as api_exception:
                api_exceptions.append(api_exception)
    else:
        # This is a single object. Call the single item method
        try:
            deleted = delete_from_yaml_single_item(
                k8s_client, data, verbose, namespace=namespace, **kwargs)
            k8s_objects.append(deleted)
        except client.rest.ApiException as api_exception:
            api_exceptions.append(api_exception)

    # In case we have exceptions waiting for us, raise them
    if api_exceptions:
        raise FailToDeleteError(api_exceptions)

    return k8s_objects

def delete_from_yaml_single_item(
        k8s_client, yml_object, verbose=False, **kwargs):
    group, _, version = yml_object["apiVersion"].partition("/")
    if version == "":
        version = group
        group = "core"
    # Take care for the case e.g. api_type is "apiextensions.k8s.io"
    # Only replace the last instance
    group = "".join(group.rsplit(".k8s.io", 1))
    # convert group name from DNS subdomain format to
    # python class name convention
    group = "".join(word.capitalize() for word in group.split('.'))
    fcn_to_call = "{0}{1}Api".format(group, version.capitalize())
    k8s_api = getattr(client, fcn_to_call)(k8s_client)
    # Replace CamelCased action_type into snake_case
    kind = yml_object["kind"]
    kind = UPPER_FOLLOWED_BY_LOWER_RE.sub(r'\1_\2', kind)
    kind = LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE.sub(r'\1_\2', kind).lower()
    # Expect the user to create namespaced objects more often
    if hasattr(k8s_api, "delete_namespaced_{0}".format(kind)):
        # Decide which namespace we are going to put the object in,
        # if any
        if "namespace" in yml_object["metadata"]:
            namespace = yml_object["metadata"]["namespace"]
            kwargs['namespace'] = namespace
        if "name" in yml_object["metadata"]:
            name = yml_object["metadata"]["name"]
            kwargs['name'] = name
        resp = getattr(k8s_api, "delete_namespaced_{0}".format(kind))(**kwargs)
    else:
        kwargs.pop('namespace', None)
        kwargs.pop('name', None)
        resp = getattr(k8s_api, "delete_{0}".format(kind))(**kwargs)
    if verbose:
        msg = "{0} deleted.".format(kind)
        if hasattr(resp, 'status'):
            msg += " status='{0}'".format(str(resp.status))
        print(msg)
    return resp

class FailToDeleteError(Exception):
    def __init__(self, api_exceptions):
        self.api_exceptions = api_exceptions

    def __str__(self):
        msg = ""
        for api_exception in self.api_exceptions:
            msg += "Error from server ({0}): {1}".format(
                api_exception.reason, api_exception.body)
        return msg


@kopf.on.create("cyberphysicalapplications")
def create_fn(spec, name, logger, **kwargs):
    k8s_client = client.ApiClient()

    deployments = spec.get("deployments")
    preferred_affinity = spec.get("requirements").get("preferredAffinity")

    deployment_affinity = None
    deployment_configs = None
    deployment_namespace = None
    deployment_app_name = None
    deployment_prometheus_url = None

    if preferred_affinity == "" or preferred_affinity is None:
        deployment_configs = deployments[0].get("configs")
        deployment_affinity = deployments[0].get("affinity")
    
    else:
        for deployment in deployments:
            deployment_affinity = deployment.get("affinity")
            if deployment_affinity == preferred_affinity:
                deployment_configs = deployment.get("configs")
                break
            
    for config in deployment_configs:
        if config.get("kind") == "Deployment":
            config["spec"]["template"]["spec"].update({"nodeSelector": {"zone" : f"{deployment_affinity}"}})
            deployment_namespace = config.get("metadata").get("namespace")
            deployment_app_name = config.get("metadata").get("labels").get("app")
            deployment_prometheus_url = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
        kopf.label(config, {"related-to": f"{name}"})
        kopf.adopt(config)
        try:
            create_from_dict(k8s_client, config)
        except Exception as e:
            logger.exception("Exception in object creation.")

    return {
        "child-deployment-namespace": deployment_namespace,
        "child-deployment-app-name": deployment_app_name,
        "child-deployment-prometheus-url": deployment_prometheus_url,
        "child-deployment-affinity": deployment_affinity
    }

def get_prometheus_odte(prometheus_url, app_name, logger):
    query_url = f"{prometheus_url}/api/v1/query?query=odte[app=\"{app_name}\"]"
    query_url = query_url.replace("[", "{").replace("]", "}")

    odte = None
    try:
        resp = requests.get(query_url)
    except:
        logger.info("Prometheus not available.")
        raise Exception
    
    try:
        odte = float(json.loads(resp.text)["data"]["result"][0]["value"][1])
    except:
        logger.info("ODTE not available.")
        raise Exception
    
    return odte

def choose_next_deployment(deployments):
    next_depl_index = random.randint(0, len(deployments) - 1)
    return deployments[next_depl_index]

def ensure_pod_termination(k8s_core_v1, app_name, namespace, logger):
    terminated = False
    label_selector = f'app={app_name}'
    while not terminated:
        try:
            resp = k8s_core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
            if len(resp.items) == 0:
                terminated = True
        except:
            logger.info("Cannot list pods.")

@kopf.daemon('cyberphysicalapplications', cancellation_backoff=1.0, cancellation_timeout=3.0, initial_delay=5)
async def check_odte(stopped, name, spec, status, logger, **kwargs):
    exception_delay = 2
    loop_delay = 1
    replacement_delay = 4

    k8s_client = client.ApiClient()
    k8s_core_v1 = client.CoreV1Api()
    
    while not stopped:
        logger.debug("Daemon listening...")

        odte_threshold = float(spec.get("requirements").get("odte"))
        deployments = spec.get("deployments")

        child_deployment_namespace = status.get("create_fn").get("child-deployment-namespace")
        child_deployment_app_name = status.get("create_fn").get("child-deployment-app-name")
        child_deployment_prometheus_url = status.get("create_fn").get("child-deployment-prometheus-url")
        current_deployment_affinity = status.get("create_fn").get("child-deployment-affinity")

        try:
            odte = get_prometheus_odte(child_deployment_prometheus_url, child_deployment_app_name, logger)
        except:
            raise kopf.TemporaryError("Exception getting odte", delay=exception_delay)
        
        logger.debug(f"Last odte read: {odte}")

        if odte is not None and odte < odte_threshold:
            logger.debug("odte below threshold")
            if len(deployments) > 1:

                try:
                    for deployment in deployments:
                        if current_deployment_affinity == deployment.get("affinity"):
                            configs = deployment.get("configs")
                            for config in configs:
                                delete_from_dict(k8s_client, config)

                    ensure_pod_termination(k8s_core_v1, child_deployment_app_name, child_deployment_namespace, logger)
                except:
                    raise kopf.TemporaryError("Exception deleting objects", delay=exception_delay)
                 
                next_deployment = choose_next_deployment(deployments)
                next_deployment_configs = next_deployment.get("configs") 
                next_deployment_affinity = next_deployment.get("affinity")

                for config in next_deployment_configs:
                    if config.get("kind") == "Deployment":
                        config["spec"]["template"]["spec"].update({"nodeSelector": {"zone" : f"{next_deployment_affinity}"}})                        
                        next_deployment_namespace = config.get("metadata").get("namespace")
                        status["create_fn"]["child-deployment-namespace"] = next_deployment_namespace
                        status["create_fn"]["child-deployment-app-name"] = config.get("metadata").get("labels").get("app")
                        status["create_fn"]["child-deployment-prometheus-url"] = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
                        status["create_fn"]["child-deployment-affinity"] = next_deployment_affinity
                    
                    kopf.adopt(config)
                    kopf.label(config, {"related-to": f"{name}"})
                    try:
                        create_from_dict(k8s_client, config)
                    except:
                        logger.exception("Exception creating replacement object.")

                logger.info(f"Replaced.")
                await stopped.wait(replacement_delay)

            else:
                logger.warn("No other deployment to switch to.")        

        await stopped.wait(loop_delay)
