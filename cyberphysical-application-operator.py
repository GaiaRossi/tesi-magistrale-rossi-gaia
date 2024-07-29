import kopf, logging, yaml, requests, json, random
from kubernetes import client, config, utils

def k8s_deploy_deployment(k8s_apps, k8s_core, deployment, namespace, name):
        for depl_config in deployment.get("configs"):
            k8sObjectType = depl_config.get("type")
            if k8sObjectType == "Deployment":
                depl_metadata = depl_config.get("metadata")
                depl_spec = depl_config.get("spec")

                # aggiunta affinity
                depl_spec["template"]["spec"].update({"nodeSelector": {"zone" : deployment.get("affinity")}})
                
                template = open("create-deployment.yml", 'rt').read()
                depl = template.format(metadata=depl_metadata, spec=depl_spec)

                data = yaml.safe_load(depl)
                kopf.label(data, {"createdFor": f"{name}"})
                resp = k8s_apps.create_namespaced_deployment(
                    body=data, namespace=namespace)
                print(f"Deployment created. Name='{resp.metadata.name}'")

def k8s_deploy_service(k8s_apps, k8s_core, deployment, namespace, name):
            for depl_config in deployment.get("configs"):
                k8sObjectType = depl_config.get("type")
                if k8sObjectType == "Service":
                    service_metadata = depl_config.get("metadata")
                    service_spec = depl_config.get("spec")
                    
                    template = open("create-service.yml", 'rt').read()
                    service = template.format(metadata=service_metadata, spec=service_spec)
                    data = yaml.safe_load(service)
                    kopf.label(data, {"createdFor": f"{name}"})
                    resp = k8s_core.create_namespaced_service(
                        body=data, namespace=namespace)
                    print(f"Service created. Name='{resp.metadata.name}'")

def k8s_delete_deployment(k8s_apps, k8s_core, deployment, namespace):
    depl_name = deployment.metadata.name
    depl_namespace = deployment.metadata.namespace

    resp = k8s_apps.delete_namespaced_deployment(depl_name, namespace=depl_namespace)
    print(f"Deployment deleted. Name='{depl_name}'")

def k8s_delete_service(k8s_apps, k8s_core, service, namespace):
    service_name = service.metadata.name
    service_namespace = service.metadata.namespace

    resp = k8s_core.delete_namespaced_service(service_name, namespace=service_namespace)
    print(f"Service deleted. Name='{service_name}'")

@kopf.on.create("cyberphysicalapplications")
def create_fn(spec, name, namespace, logger, **kwargs):
    # config.load_kube_config()

    k8s_apps_v1 = client.AppsV1Api()
    k8s_core_v1 = client.CoreV1Api()

    deployments = spec.get("deployments")
    preferred_affinity = spec.get("requirements").get("preferredAffinity")

    if preferred_affinity == "":
        deployment_type = deployments[0].get("type")
        if deployment_type == "Kubernetes":
            k8s_deploy_deployment(k8s_apps_v1, k8s_core_v1, deployments[0], namespace, name)
            k8s_deploy_service(k8s_apps_v1, k8s_core_v1, deployments[0], namespace, name)
  
    else:
        for deployment in deployments:
            deployment_type = deployment.get("type")
            deployment_affinity = deployment.get("affinity")
            if deployment_type == "Kubernetes" and deployment_affinity == preferred_affinity:
                k8s_deploy_deployment(k8s_apps_v1, k8s_core_v1, deployment, namespace, name)
                k8s_deploy_service(k8s_apps_v1, k8s_core_v1, deployment, namespace, name)



@kopf.on.delete('cyberphysicalapplications')
def delete_fn(spec, name, namespace, logger, **kwargs):
    k8s_apps_v1 = client.AppsV1Api()
    k8s_core_v1 = client.CoreV1Api()

    label_selector = f"createdFor={name}"
    resp = k8s_apps_v1.list_namespaced_deployment(namespace, label_selector=label_selector)
    current_depl = resp.items[0]
    k8s_delete_deployment(k8s_apps_v1, k8s_core_v1, current_depl, namespace)

    depl_namespace = current_depl.metadata.namespace
    label_selector = f"createdFor={name}"
    resp = k8s_core_v1.list_namespaced_service(depl_namespace, label_selector=label_selector)
    service = resp.items[0]
    k8s_delete_service(k8s_apps_v1, k8s_core_v1, service, namespace)


@kopf.on.update('cyberphysicalapplications')
def update_cpa(body, **kwargs):
    pass

@kopf.daemon('cyberphysicalapplications', cancellation_backoff=1.0, cancellation_timeout=3.0, initial_delay=5)
async def check_odte(stopped, name, spec, namespace, body, logger, **kwargs):
    while not stopped:
        logger.info("Daemon listening...")
        k8s_apps_v1 = client.AppsV1Api()
        k8s_core_v1 = client.CoreV1Api()
        preferredAffinity = body.get("spec").get("requirements").get("preferredAffinity")
        odte_threshold = float(body.get("spec").get("requirements").get("odte"))
        deployments = body.get("spec").get("deployments")
        current_deployment = None
        prometheus_url = None

        for deployment in deployments:
            if deployment.get("affinity") == preferredAffinity:
                current_deployment = deployment

        configs = current_deployment.get("configs")
        for config in configs:
            if config.get("type") == "Deployment":
                prometheus_url = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
                app_name = config.get("spec").get("template").get("metadata").get("labels").get("app")
                depl_name = config.get("metadata").get("name")
                depl_namespace = config.get("metadata").get("namespace")
        
        query_url = f"{prometheus_url}/api/v1/query?query=odte[app=\"{app_name}\"]"
        query_url = query_url.replace("[", "{").replace("]", "}")
        try:
            resp = requests.get(query_url)
        except:
            logger.info("Prometheus not available")
        try:
            odte = float(json.loads(resp.text)["data"]["result"][0]["value"][1])
        except:
            logger.info("ODTE not available")
            odte = None

        logger.debug(f"Last odte read: {odte}")

        if odte and odte < odte_threshold:
            logger.info("odte below threshold")
            if len(deployments) > 1:
                next_depl = random.randint(0, len(deployments) - 1)
                try:
                    resp = k8s_apps_v1.delete_namespaced_deployment(depl_name, namespace=depl_namespace)
                except:
                    pass
                logger.info(f"Deployment deleted. Name='{depl_name}'")

                # attendo terminazione pod
                terminated = False
                label_selector = f'app={app_name}'
                while not terminated:
                    try:
                        resp = k8s_core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                        if len(resp.items) == 0:
                            terminated = True
                    except:
                        logger.info("Cannot list pods")
                

                deployment = deployments[next_depl]
                for depl_config in deployment.get("configs"):
                    k8sObjectType = depl_config.get("type")
                    if k8sObjectType == "Deployment":
                        depl_metadata = depl_config.get("metadata")
                        depl_spec = depl_config.get("spec")

                        # aggiunta affinity
                        depl_spec["template"]["spec"].update({"nodeSelector": {"zone" : deployment.get("affinity")}})
                        
                        template = open("create-deployment.yml", 'rt').read()
                        depl = template.format(metadata=depl_metadata, spec=depl_spec)

                        data = yaml.safe_load(depl)

                        try:
                            kopf.label(data, {"createdFor": f"{name}"})
                            resp = k8s_apps_v1.create_namespaced_deployment(
                                body=data, namespace=namespace)
                            logger.info(f"Deployment created. Name='{resp.metadata.name}'")
                        except:
                            logger.info("Exception creating replace deployment")
        

        await stopped.wait(1)

