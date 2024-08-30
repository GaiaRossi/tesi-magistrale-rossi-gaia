import kopf, logging, yaml, requests, json, random, re
from kubernetes import client, config, utils
from kubernetes.utils import create_from_dict

@kopf.on.create("cyberphysicalapplications")
def create_fn(spec, name, namespace, logger, **kwargs):
    k8s_client = client.ApiClient()

    deployments = spec.get("deployments")
    preferred_affinity = spec.get("requirements").get("preferredAffinity")

    deployment_affinity = None
    deployment_configs = None

    if preferred_affinity == "":
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
        kopf.label(config, {"createdFor": f"{name}"})
        kopf.adopt(config)
        try:
            create_from_dict(k8s_client, config)
        except Exception as e:
            logger.exception("Exception in object creation.")
  

@kopf.on.delete('cyberphysicalapplications')
def delete_fn(spec, name, namespace, logger, **kwargs):
    pass


@kopf.on.update('cyberphysicalapplications')
def update_fn(body, **kwargs):
    pass


@kopf.daemon('cyberphysicalapplications', cancellation_backoff=1.0, cancellation_timeout=3.0, initial_delay=5)
async def check_odte(stopped, name, spec, namespace, body, logger, **kwargs):
    while not stopped:
        logger.info("Daemon listening...")

        k8s_apps_v1 = client.AppsV1Api()
        k8s_core_v1 = client.CoreV1Api()

        odte_threshold = float(spec.get("requirements").get("odte"))
        deployments = spec.get("deployments")

        current_depl = None
        label_selector = f"createdFor={name}"
        try:
            resp = k8s_apps_v1.list_namespaced_deployment(namespace, label_selector=label_selector)
            current_depl = resp.items[0]
        except:
            logger.info("No deployment found.")
            await stopped.wait(1)
            continue

        app_name = current_depl.metadata.labels["app"]
        prometheus_url = current_depl.spec.template.metadata.annotations["prometheusUrl"]
        query_url = f"{prometheus_url}/api/v1/query?query=odte[app=\"{app_name}\"]"
        query_url = query_url.replace("[", "{").replace("]", "}")
        try:
            resp = requests.get(query_url)
        except:
            logger.info("Prometheus not available.")
            await stopped.wait(1)
            continue
        
        try:
            odte = float(json.loads(resp.text)["data"]["result"][0]["value"][1])
        except:
            logger.info("ODTE not available.")
            odte = None
            await stopped.wait(1)
            continue

        logger.debug(f"Last odte read: {odte}")

        if odte is not None and odte < odte_threshold:
            logger.debug("odte below threshold")
            if len(deployments) > 1:
                try:
                    resp = k8s_apps_v1.delete_namespaced_deployment(current_depl.metadata.name, namespace=current_depl.metadata.namespace)
                except:
                    logger.exception("Exception deleting deployment.")

                terminated = False
                label_selector = f'app={app_name}'
                while not terminated:
                    try:
                        resp = k8s_core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                        if len(resp.items) == 0:
                            terminated = True
                    except:
                        logger.info("Cannot list pods.")
                

                next_depl = random.randint(0, len(deployments) - 1)
                deployment = deployments[next_depl]
                deployment_configs = deployment.get("configs") 
                deployment_affinity = deployment.get("affinity")

                for config in deployment_configs:
                    if config.get("kind") == "Deployment":
                        config["spec"]["template"]["spec"].update({"nodeSelector": {"zone" : f"{deployment_affinity}"}})
                        kopf.adopt(config)
                        kopf.label(config, {"createdFor": f"{name}"})
                        try:
                            resp = k8s_apps_v1.create_namespaced_deployment(namespace, config)
                            logger.info(f"Deployment replaced.")
                        except:
                            logger.exception("Exception creating replace deployment.")
        

        await stopped.wait(1)

