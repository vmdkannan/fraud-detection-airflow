import boto3
import time
from datetime import datetime
from airflow.decorators import task
from airflow.models.dag import DAG
from airflow.providers.amazon.aws.operators.ec2 import (
    EC2CreateInstanceOperator,
    EC2TerminateInstanceOperator,
)
from airflow.hooks.base import BaseHook
import paramiko
from airflow.utils.trigger_rule import TriggerRule
from airflow.models import Variable
from evidently import EvidentlyClient
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
import pandas as pd


aws_conn = BaseHook.get_connection('aws_default')  # Use the Airflow AWS connection
aws_access_key_id = aws_conn.login
aws_secret_access_key = aws_conn.password
region_name = aws_conn.extra_dejson.get('region_name', 'eu-west-3')  # Default to 'eu-west-3'


AWS_ACCESS_KEY_ID= aws_access_key_id
AWS_SECRET_ACCESS_KEY=aws_secret_access_key
BUCKET_NAME = Variable.get("BUCKET_NAME")
FILE_KEY = Variable.get("FILE_KEY")
ARTIFACT_ROOT = Variable.get("ARTIFACT_ROOT")


# # Define constants for Evidently configuration
# EVIDENTLY_BASE_URL = "https://api.evidentlyai.cloud"
# EVIDENTLY_PROJECT_ID = "019358ac-77c0-7138-a389-4ce4de470692"

# # S3 Bucket and Key Details (Updated for cv_results.csv)
# RESULT_FILE_KEY = "training-models/3/76a94082e1884f68b2a7f63d61424109/artifacts/cv_results.csv"
# LOCAL_FILE_PATH = "/tmp/cv_results.csv"  # Temp location to save file from S3

# Define constants for Evidently configuration
EVIDENTLY_BASE_URL = Variable.get("EVIDENTLY_BASE_URL")
EVIDENTLY_PROJECT_ID = Variable.get("EVIDENTLY_PROJECT_ID")

# S3 Bucket and Key Details (Updated for cv_results.csv)
RESULT_FILE_KEY = Variable.get("RESULT_FILE_KEY")
LOCAL_FILE_PATH = Variable.get("LOCAL_FILE_PATH")



# DAG Configuration
DAG_ID = 'fd_training_data_reporting'
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2023, 1, 1),
    'retries': 1,
}

# Define the DAG
with DAG(
    dag_id=DAG_ID,
    schedule_interval=None,
    default_args=default_args,
    description="Send training data to Evidently AI Cloud",
    catchup=False,
    tags=['S3', 'Evidently'],
) as dag:
    
    
    @task
    def download_data_from_s3():
        """Download the cv_results.csv file from S3."""
        try:
            # Initialize S3 Hook
            s3_hook = S3Hook(aws_conn_id='aws_default')  # Specify your AWS connection ID if needed
            
            # Download the file from S3
            s3_hook.download_file(
                key=RESULT_FILE_KEY,
                bucket_name=BUCKET_NAME,
                local_path=LOCAL_FILE_PATH
            )
            
            print(f"File downloaded from S3 and saved to {LOCAL_FILE_PATH}")

        except Exception as e:
            print(f"Error occurred during S3 file download: {str(e)}")
            raise

    @task
    def send_data_to_evidently():
        """Send ML training data to Evidently AI Cloud for analysis."""
        try:
            # Load Evidently API Token inside the function
            evidently_api_token = Variable.get("EVIDENTLY_API_TOKEN")

            # Load training data from the downloaded file
            data = pd.read_csv(LOCAL_FILE_PATH)  # Load the data you want to analyze

            # Initialize Evidently Client
            client = EvidentlyClient(api_token=evidently_api_token, base_url=EVIDENTLY_BASE_URL)

            # Upload data to Evidently Cloud
            client.upload_data(
                project_id=EVIDENTLY_PROJECT_ID,
                dataset_name="cv_results_data",
                data=data
            )

            # Generate a report (e.g., Data Drift Report)
            client.create_report(
                project_id=EVIDENTLY_PROJECT_ID,
                report_type="data_drift",
                reference_data=data,  # Can split into reference/current data if needed
                current_data=data
            )
            
            print("Data successfully sent to Evidently AI Cloud!")

        except Exception as e:
            print(f"Error occurred while sending data to Evidently: {str(e)}")
            raise
        
        
    download_task = download_data_from_s3()
    reporting_task = send_data_to_evidently()

    # Ensure tasks run in the correct order
    download_task >> reporting_task