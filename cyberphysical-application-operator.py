import kopf, requests, json, random
from kubernetes import client
from kubernetes.utils import create_from_dict

@kopf.on.create("cyberphysicalapplications")
def create_fn(spec, name, logger, **kwargs):
    k8s_client = client.ApiClient()

    deployments = spec.get("deployments")
    preferred_affinity = spec.get("requirements").get("preferredAffinity")

    deployment_affinity = None
    deployment_configs = None
    deployment_name = None
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
            deployment_name = config.get("metadata").get("name")
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
        "child-deployment-name": deployment_name,
        "child-deployment-namespace": deployment_namespace,
        "child-deployment-app-name": deployment_app_name,
        "child-deployment-prometheus-url": deployment_prometheus_url
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

def delete_current_deployment(k8s_apps_v1, depl_name, depl_namespace, logger):
    try:
        k8s_apps_v1.delete_namespaced_deployment(depl_name, depl_namespace)
    except:
        logger.exception("Exception deleting deployment.")
        raise Exception

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
async def check_odte(stopped, name, spec, status, namespace, logger, **kwargs):
    exception_delay = 2
    loop_delay = 1
    replacement_delay = 4

    k8s_apps_v1 = client.AppsV1Api()
    k8s_core_v1 = client.CoreV1Api()
    
    while not stopped:
        logger.debug("Daemon listening...")

        odte_threshold = float(spec.get("requirements").get("odte"))
        deployments = spec.get("deployments")

        child_deployment_name = status.get("create_fn").get("child-deployment-name")
        child_deployment_namespace = status.get("create_fn").get("child-deployment-namespace")
        child_deployment_app_name = status.get("create_fn").get("child-deployment-app-name")
        child_deployment_prometheus_url = status.get("create_fn").get("child-deployment-prometheus-url")

        try:
            odte = get_prometheus_odte(child_deployment_prometheus_url, child_deployment_app_name, logger)
        except:
            raise kopf.TemporaryError("Exception getting odte", delay=exception_delay)
        
        logger.debug(f"Last odte read: {odte}")

        if odte is not None and odte < odte_threshold:
            logger.debug("odte below threshold")
            if len(deployments) > 1:
                
                try:
                    delete_current_deployment(k8s_apps_v1, child_deployment_name, child_deployment_namespace, logger)
                    ensure_pod_termination(k8s_core_v1, child_deployment_app_name, child_deployment_namespace, logger)
                except:
                    raise kopf.TemporaryError("Exception deleting current deployment", delay=exception_delay)
                 
                next_deployment = choose_next_deployment(deployments)
                next_deployment_configs = next_deployment.get("configs") 
                next_deployment_affinity = next_deployment.get("affinity")

                for config in next_deployment_configs:
                    if config.get("kind") == "Deployment":
                        config["spec"]["template"]["spec"].update({"nodeSelector": {"zone" : f"{next_deployment_affinity}"}})
                        kopf.adopt(config)
                        kopf.label(config, {"related-to": f"{name}"})
                        
                        next_deployment_namespace = config.get("metadata").get("namespace")

                        status["create_fn"]["child-deployment-name"] = config.get("metadata").get("name")
                        status["create_fn"]["child-deployment-namespace"] = next_deployment_namespace
                        status["create_fn"]["child-deployment-app-name"] = config.get("metadata").get("labels").get("app")
                        status["create_fn"]["child-deployment-prometheus-url"] = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
                        
                        try:
                            k8s_apps_v1.create_namespaced_deployment(next_deployment_namespace, config)
                            logger.info(f"Deployment replaced.")
                            await stopped.wait(replacement_delay)
                            continue
                        except:
                            logger.exception("Exception creating replacement deployment.")

            else:
                logger.warn("No other deployment to switch to.")        

        await stopped.wait(loop_delay)
