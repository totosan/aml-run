import os
import sys
import json
import importlib

from azureml.core import Workspace, Experiment
from azureml.core.authentication import ServicePrincipalAuthentication
from azureml.pipeline.core import PipelineRun
from azureml.exceptions import AuthenticationException, ProjectSystemException, AzureMLException, UserErrorException
from adal.adal_error import AdalError
from msrest.exceptions import AuthenticationError
from json import JSONDecodeError
from utils import AMLConfigurationException, AMLExperimentConfigurationException, required_parameters_provided, mask_parameter


def main():
    # Loading input values
    print("::debug::Loading input values")
    parameters_file = os.environ.get("INPUT_PARAMETERS_FILE", default="run.json")
    azure_credentials = os.environ.get("INPUT_AZURE_CREDENTIALS", default="{}")
    try:
        azure_credentials = json.loads(azure_credentials)
    except JSONDecodeError:
        print("::error::Please paste output of `az ad sp create-for-rbac --name <your-sp-name> --role contributor --scopes /subscriptions/<your-subscriptionId>/resourceGroups/<your-rg> --sdk-auth` as value of secret variable: AZURE_CREDENTIALS")
        raise AMLConfigurationException(f"Incorrect or poorly formed output from azure credentials saved in AZURE_CREDENTIALS secret. See setup in https://github.com/Azure/aml-workspace/blob/master/README.md")

    # Checking provided parameters
    print("::debug::Checking provided parameters")
    required_parameters_provided(
        parameters=azure_credentials,
        keys=["tenantId", "clientId", "clientSecret"],
        message="Required parameter(s) not found in your azure credentials saved in AZURE_CREDENTIALS secret for logging in to the workspace. Please provide a value for the following key(s): "
    )

    # Mask values
    print("::debug::Masking parameters")
    mask_parameter(parameter=azure_credentials.get("tenantId", ""))
    mask_parameter(parameter=azure_credentials.get("clientId", ""))
    mask_parameter(parameter=azure_credentials.get("clientSecret", ""))
    mask_parameter(parameter=azure_credentials.get("subscriptionId", ""))

    # Loading parameters file
    print("::debug::Loading parameters file")
    parameters_file_path = os.path.join(".cloud", ".azure", parameters_file)
    try:
        with open(parameters_file_path) as f:
            parameters = json.load(f)
    except FileNotFoundError:
        print(f"::debug::Could not find parameter file in {parameters_file_path}. Please provide a parameter file in your repository  if you do not want to use default settings (e.g. .cloud/.azure/run.json).")
        parameters = {}

    # Loading Workspace
    print("::debug::Loading AML Workspace")
    sp_auth = ServicePrincipalAuthentication(
        tenant_id=azure_credentials.get("tenantId", ""),
        service_principal_id=azure_credentials.get("clientId", ""),
        service_principal_password=azure_credentials.get("clientSecret", "")
    )
    config_file_path = os.environ.get("GITHUB_WORKSPACE", default=".cloud/.azure")
    config_file_name = "aml_arm_config.json"
    try:
        ws = Workspace.from_config(
            path=config_file_path,
            _file_name=config_file_name,
            auth=sp_auth
        )
    except AuthenticationException as exception:
        print(f"::error::Could not retrieve user token. Please paste output of `az ad sp create-for-rbac --name <your-sp-name> --role contributor --scopes /subscriptions/<your-subscriptionId>/resourceGroups/<your-rg> --sdk-auth` as value of secret variable: AZURE_CREDENTIALS: {exception}")
        raise AuthenticationException
    except AuthenticationError as exception:
        print(f"::error::Microsoft REST Authentication Error: {exception}")
        raise AuthenticationError
    except AdalError as exception:
        print(f"::error::Active Directory Authentication Library Error: {exception}")
        raise AdalError
    except ProjectSystemException as exception:
        print(f"::error::Workspace authorizationfailed: {exception}")
        raise ProjectSystemException

    # Create experiment
    print("::debug::Creating experiment")
    try:
        # Default experiment name
        repository_name = os.environ.get("GITHUB_REPOSITORY").split("/")[-1]
        branch_name = os.environ.get("GITHUB_REF").split("/")[-1]
        default_experiment_name = f"{repository_name}-{branch_name}"

        experiment = Experiment(
            workspace=ws,
            name=parameters.get("experiment_name", default_experiment_name)
        )
    except TypeError as exception:
        experiment_name = parameters.get("experiment", None)
        print(f"::error::Could not create an experiment with the specified name {experiment_name}: {exception}")
        raise AMLExperimentConfigurationException(f"Could not create an experiment with the specified name {experiment_name}: {exception}")
    except UserErrorException as exception:
        experiment_name = parameters.get("experiment", None)
        print(f"::error::Could not create an experiment with the specified name {experiment_name}: {exception}")
        raise AMLExperimentConfigurationException(f"Could not create an experiment with the specified name {experiment_name}: {exception}")

    # if parameters.get("pipeline_yaml_file", None) is not None:
    #     # Load pipeline yaml definition
    #     print("::debug::Loading pipeline yaml definition")
    #     try:
    #         run_config = Pipeline.load_yaml(
    #             workspace=ws,
    #             filename=parameters.get("pipeline_yaml_file", "code/train/pipeline.yml")
    #         )
    #     except Exception as exception:
    #         pipeline_yaml_path = parameters.get("pipeline_yaml_file", None)
    #         print(f"::error::Error when loading pipeline yaml definition your repository (Path: /{pipeline_yaml_path}): {exception}")
    #         raise AMLExperimentConfigurationException(f"Error when loading pipeline yaml definition your repository (Path: /{pipeline_yaml_path}): {exception}")

    # Load module
    print("::debug::Loading module to receive experiment config")
    root = os.environ.get("GITHUB_WORKSPACE", default=None)
    run_config_file_path = parameters.get("run_config_file_path", "code/train/run_config.py")
    run_config_file_function_name = parameters.get("run_config_file_function_name", "main")

    print("::debug::Adding root to system path")
    sys.path.insert(1, f"{root}")

    print("::debug::Importing module")
    run_config_file_path = f"{run_config_file_path}.py" if not run_config_file_path.endswith(".py") else run_config_file_path
    try:
        run_config_spec = importlib.util.spec_from_file_location(
            name="runmodule",
            location=run_config_file_path
        )
        run_config_module = importlib.util.module_from_spec(spec=run_config_spec)
        run_config_spec.loader.exec_module(run_config_module)
        run_config_function = getattr(run_config_module, run_config_file_function_name, None)
    except ModuleNotFoundError as exception:
        print(f"::error::Could not load python script in your repository which defines the experiment config (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")
        raise AMLExperimentConfigurationException(f"Could not load python script in your repository which defines the experiment config (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")
    except FileNotFoundError as exception:
        print(f"::error::Could not load python script or function in your repository which defines the experiment config (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")
        raise AMLExperimentConfigurationException(f"Could not load python script or function in your repository which defines the experiment config (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")
    except AttributeError as exception:
        print(f"::error::Could not load python script or function in your repository which defines the experiment config (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")
        raise AMLExperimentConfigurationException(f"Could not load python script or function in your repository which defines the experiment config (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")

    # Load experiment config
    print("::debug::Loading experiment config")
    try:
        run_config = run_config_function(ws)
    except TypeError as exception:
        print(f"::error::Could not load experiment config from your module (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")
        raise AMLExperimentConfigurationException(f"Could not load experiment config from your module (Script: /{run_config_file_path}, Function: {run_config_file_function_name}()): {exception}")

    # Submit experiment config
    print("::debug::Submitting experiment config")
    try:
        run = experiment.submit(
            config=run_config,
            tags=parameters.get("tags", {})
        )
    except AzureMLException as exception:
        print(f"::error::Could not submit experiment config. Your script passed object of type {type(run_config)}. Object must be correctly configured and of type e.g. estimator, pipeline, etc.: {exception}")
        raise AMLExperimentConfigurationException(f"Could not submit experiment config. Your script passed object of type {type(run_config)}. Object must be correctly configured and of type e.g. estimator, pipeline, etc.: {exception}")
    except TypeError as exception:
        print(f"::error::Could not submit experiment config. Your script passed object of type {type(run_config)}. Object must be correctly configured and of type e.g. estimator, pipeline, etc.: {exception}")
        raise AMLExperimentConfigurationException(f"Could not submit experiment config. Your script passed object of type {type(run_config)}. Object must be correctly configured and of type e.g. estimator, pipeline, etc.: {exception}")

    # Create outputs
    print("::debug::Creating outputs")
    print(f"::set-output name=experiment_name::{run.experiment.name}")
    print(f"::set-output name=run_id::{run.id}")
    print(f"::set-output name=run_url::{run.get_portal_url()}")

    # Waiting for run to complete
    print("::debug::Waiting for run to complete")
    if parameters.get("wait_for_completion", True):
        run.wait_for_completion(show_output=True)

        # Creating additional outputs of finished run
        run_metrics = run.get_metrics(recursive=True)
        # metrics_markdown = convert_to_markdown(run_metrics)
        # print(f"::set-output name=run_metrics::{metrics_markdown}")
        print(f"::set-output name=run_metrics::{run_metrics}")

    # Publishing pipeline
    print("::debug::Publishing pipeline")
    if type(run) is PipelineRun and parameters.get("publish_pipeline", False):
        # Default pipeline name
        repository_name = os.environ.get("GITHUB_REPOSITORY").split("/")[-1]
        branch_name = os.environ.get("GITHUB_REF").split("/")[-1]
        default_pipeline_name = f"{repository_name}-{branch_name}"

        published_pipeline = run.publish_pipeline(
            name=parameters.get("pipeline_name", default_pipeline_name),
            description="Pipeline registered by GitHub Run Action",
            version=parameters.get("pipeline_version", None),
            continue_on_step_failure=parameters.get("pipeline_continue_on_step_failure", False)
        )

        # Creating additional outputs
        print(f"::set-output name=published_pipeline_id::{published_pipeline.id}")
        print(f"::set-output name=published_pipeline_status::{published_pipeline.status}")
        print(f"::set-output name=published_pipeline_endpoint::{published_pipeline.endpoint}")
    elif parameters.get("publish_pipeline", False):
        print(f"::error::Could not register pipeline because you did not pass a pipeline to the action")

    print("::debug::Successfully finished Azure Machine Learning Train Action")


if __name__ == "__main__":
    main()
