import boto3
import time
from datetime import datetime
from airflow.decorators import task
from airflow.models.dag import DAG
from airflow.providers.amazon.aws.operators.ec2 import (
    EC2CreateInstanceOperator,
    EC2TerminateInstanceOperator,
)
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.hooks.base import BaseHook
import paramiko
from airflow.utils.trigger_rule import TriggerRule
from airflow.models import Variable
import os
from airflow.configuration import conf
import logging
from io import StringIO, BytesIO



# Jenkins Configuration: Load from Airflow Variables
JENKINS_URL = Variable.get("JENKINS_URL")
JENKINS_USER = Variable.get("JENKINS_USER")
JENKINS_TOKEN = Variable.get("JENKINS_TOKEN")
JENKINS_JOB_NAME = Variable.get("JENKINS_JOB_NAME")

# Get AWS connection details from Airflow
KEY_PAIR_NAME=Variable.get("KEY_PAIR_NAME")
KEY_PATH = Variable.get("KEY_PATH")  # Path to your private key inside the container
AMI_ID=Variable.get("AMI_ID")
SECURITY_GROUP_ID=Variable.get("SECURITY_GROUP_ID")
INSTANCE_TYPE=Variable.get("INSTANCE_TYPE")
aws_conn = BaseHook.get_connection('aws_default')  # Use the Airflow AWS connection
aws_access_key_id = aws_conn.login
aws_secret_access_key = aws_conn.password
region_name = aws_conn.extra_dejson.get('region_name', 'eu-west-3')  # Default to 'eu-west-3'

# Retrieve other env variables for MLFlow to run
MLFLOW_TRACKING_URI=Variable.get("MLFLOW_TRACKING_URI")
MLFLOW_EXPERIMENT_ID=Variable.get("MLFLOW_EXPERIMENT_ID")
AWS_ACCESS_KEY_ID= aws_access_key_id
AWS_SECRET_ACCESS_KEY=aws_secret_access_key
BUCKET_NAME = Variable.get("BUCKET_NAME")
FILE_KEY = Variable.get("FILE_KEY")
ARTIFACT_ROOT = Variable.get("ARTIFACT_ROOT")


if not all([JENKINS_URL, JENKINS_USER, JENKINS_TOKEN]):
    raise ValueError("Missing one or more Jenkins configuration environment variables")

# DAG Configuration
DAG_ID = 'fd_jenkins_ec2_training_dag'
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2025, 1, 1),
    'retries': 1,
}

# Define the DAG
with DAG(
    dag_id=DAG_ID,
    schedule_interval='0 2 * * *',
    default_args=default_args,
    description="Poll Jenkins, launch EC2, and run ML training",
    catchup=False,
    tags=['jenkins', 'ec2', 'ml-training'],
) as dag:

    # Step 1: Poll Jenkins Job Status
    @task
    def poll_jenkins_job():
        """Poll Jenkins for the job status and check for successful build."""
        import requests
        import time

        # Step 1: Get the latest build number from the job API
        job_url = f"{JENKINS_URL}/job/{JENKINS_JOB_NAME}/api/json"
        response = requests.get(job_url, auth=(JENKINS_USER, JENKINS_TOKEN))
        if response.status_code != 200:
            raise Exception(f"Failed to query Jenkins API: {response.status_code}")

        job_info = response.json()
        latest_build_number = job_info['lastBuild']['number']

        # Step 2: Poll the latest build's status
        build_url = f"{JENKINS_URL}/job/{JENKINS_JOB_NAME}/{latest_build_number}/api/json"
        
        while True:
            response = requests.get(build_url, auth=(JENKINS_USER, JENKINS_TOKEN))
            if response.status_code == 200:
                build_info = response.json()
                if not build_info['building']:  # Build is finished
                    if build_info['result'] == 'SUCCESS':
                        print("Jenkins build successful!")
                        return True
                    else:
                        raise Exception("Jenkins build failed!")
            else:
                raise Exception(f"Failed to query Jenkins API: {response.status_code}")
            
            time.sleep(30)  # Poll every 30 seconds

    # Step 2: Create EC2 Instance Using EC2 Operator
    create_ec2_instance = EC2CreateInstanceOperator(
        task_id="create_ec2_instance",
        image_id= AMI_ID,  
        max_count=1,
        min_count=1,
        config={  # Dictionary for arbitrary parameters to the boto3 `run_instances` call
            "InstanceType": INSTANCE_TYPE,
            "KeyName": KEY_PAIR_NAME,  
            "SecurityGroupIds": [SECURITY_GROUP_ID],  
            "TagSpecifications": [
                {
                    'ResourceType': 'instance',
                    'Tags': [{'Key': 'Purpose', 'Value': 'ML-Training'}]
                }
            ]
        },
        wait_for_completion=True,  # Wait for the instance to be running before proceeding
    )

    # Step 3: Use EC2 Sensor to Check if Instance is Running
    @task
    def check_ec2_status(instance_id):
        """Check if the EC2 instance has passed both status checks (2/2 checks passed)."""
        
        ec2_client = boto3.client(
            'ec2', 
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name
        )  
        passed_checks = False
        
        while not passed_checks:
            # Get the instance status
            response = ec2_client.describe_instance_status(InstanceIds=instance_id)

            # Check if there is any status information returned
            if response['InstanceStatuses']:
                instance_status = response['InstanceStatuses'][0]

                system_status = instance_status['SystemStatus']['Status']
                instance_status_check = instance_status['InstanceStatus']['Status']
                
                # Log the current status
                print(f"System Status: {system_status}, Instance Status: {instance_status_check}")
                
                # Check if both status checks are passed
                if system_status == 'ok' and instance_status_check == 'ok':
                    print(f"Instance {instance_id} has passed 2/2 status checks.")
                    passed_checks = True
                else:
                    print(f"Waiting for instance {instance_id} to pass 2/2 status checks...")
            else:
                print(f"No status available for instance {instance_id} yet. Waiting...")

            # Wait before polling again
            time.sleep(15)

        return True

    # Step 4: Define Run Training as an @task to Get EC2 Public IP
    @task
    def get_ec2_public_ip(instance_id):
        """Retrieve the EC2 instance public IP for SSH."""
    

        # Initialize the EC2 resource using boto3 with credentials from Airflow connection
        ec2 = boto3.resource(
            'ec2',
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name
        )

        # Access EC2 instance by ID
        instance = ec2.Instance(instance_id[0])

        # Wait for the instance to be running
        instance.wait_until_running()
        instance.reload()

        # Get the instance's public IP
        public_ip = instance.public_ip_address
        print(f"Public IP of EC2 Instance: {public_ip}")

        # Return the public IP for the SSH task
        return public_ip


    #  Step 5: Run mlflow and start train model as a @task 
    @task
    def run_training_via_paramiko(public_ip):
        """Use Paramiko to SSH into the EC2 instance and run ML training."""

        print("PUBLIC IP:",public_ip)
        # Initialize SSH client
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # Automatically add unknown hosts

        # Load private key
        private_key = paramiko.RSAKey.from_private_key_file(KEY_PATH)

        try:
            # Establish an SSH connection
            ssh_client.connect(hostname=public_ip, username='ubuntu', pkey=private_key)

            # Export environment variables 
            command = f"""
            export MLFLOW_TRACKING_URI={MLFLOW_TRACKING_URI}
            export MLFLOW_EXPERIMENT_ID={MLFLOW_EXPERIMENT_ID}
            export AWS_ACCESS_KEY_ID={AWS_ACCESS_KEY_ID}
            export AWS_SECRET_ACCESS_KEY={AWS_SECRET_ACCESS_KEY}
            export ARTIFACT_ROOT={ARTIFACT_ROOT}
            export BUCKET_NAME={BUCKET_NAME}
            export FILE_KEY={FILE_KEY}
            export PATH=$PATH:/home/ubuntu/.local/bin
            mlflow run https://github.com/VeeraK81/pipeline-workflow --build-image    
            """
            
            # Run your training command via SSH
            stdin, stdout, stderr = ssh_client.exec_command(command)
            
            # Print the output for debugging
            for line in stdout:
                print(line.strip())
            for line in stderr:
                print(line.strip())

        except Exception as e:
            print(f"Error occurred during SSH: {str(e)}")
            raise
        finally:
            # Close the SSH connection
            ssh_client.close()


    # Step 6: Terminate EC2 Instance
    terminate_instance = EC2TerminateInstanceOperator(
        task_id="terminate_ec2_instance",
        instance_ids="{{ task_instance.xcom_pull(task_ids='create_ec2_instance', key='return_value')[0] }}",
        wait_for_completion=True,
        trigger_rule=TriggerRule.ALL_DONE,
    )
    
    

    # Step 7: Send logs to S3 bucket
    @task
    def write_logs_s3():
        # S3 Configuration
        s3_hook = S3Hook(aws_conn_id="aws_default")
        S3_BUCKET_NAME = BUCKET_NAME
        S3_KEY_PREFIX = "logs/airflow_fraud_detection_logs"

        # Base log folder from Airflow configuration
        base_log_folder = conf.get('logging', 'base_log_folder')

        # Get today's date
        today = datetime.utcnow().date()

        # Current timestamp for unique file naming
        timestamp = datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')
        consolidated_log_file = f"airflow_fd_logs_{timestamp}.txt"

        try:
            # Use StringIO for in-memory log consolidation (avoid writing to disk)
            log_content = StringIO()
            logging.info(f"Collecting today's logs from {base_log_folder}...")

            # Walk through the base log folder and filter today's logs based on file modification date
            for root, dirs, files in os.walk(base_log_folder):
                for file in files:
                    log_path = os.path.join(root, file)

                    # Check if the log file was modified today
                    try:
                        file_modified_time = datetime.utcfromtimestamp(os.path.getmtime(log_path)).date()
                        if file_modified_time == today:
                            log_content.write(f"--- Log file: {log_path} ---\n")
                            with open(log_path, 'r') as f:
                                log_content.write(f.read())
                                log_content.write("\n\n")
                    except Exception as e:
                        logging.warning(f"Could not read or process log file {log_path}: {str(e)}")

            # If there are logs collected for today
            if log_content.tell() > 0:
                # Reset the StringIO object to the beginning before reading
                log_content.seek(0)

                # Convert the StringIO content to bytes
                log_bytes = log_content.getvalue().encode('utf-8')

                # Wrap the bytes in a BytesIO object to make it file-like
                log_file_obj = BytesIO(log_bytes)

                # Upload the consolidated log content to S3 directly
                s3_key = f"{S3_KEY_PREFIX}/{consolidated_log_file}"
                logging.info(f"Uploading consolidated log file to S3: {S3_BUCKET_NAME}/{s3_key}")
                s3_hook.load_file_obj(
                    file_obj=log_file_obj,
                    key=s3_key,
                    bucket_name=S3_BUCKET_NAME,
                    replace=True
                )

                logging.info("Today's logs uploaded to S3.")
            else:
                logging.info("No logs found for today.")

        except Exception as e:
            logging.error(f"Error during log collection or S3 upload: {str(e)}")
            raise

    

#     # Task Chaining (DAG Workflow)
    jenkins_poll = poll_jenkins_job()
    ec2_public_ip = get_ec2_public_ip(create_ec2_instance.output)
    check_ec2_instance=check_ec2_status(create_ec2_instance.output)
    ssh_training_task = run_training_via_paramiko(ec2_public_ip)
    # Task Chaining (DAG Workflow)
    write_logs_task = write_logs_s3()
    
    # Define the task flow sequence in the pipeline    
    jenkins_poll >> create_ec2_instance >> check_ec2_instance >> ec2_public_ip >> ssh_training_task >> terminate_instance >> write_logs_task

