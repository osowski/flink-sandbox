import json
import boto3


def fetch_confluent_credentials(secret_path: str, aws_region: str) -> dict:
    client = boto3.client("secretsmanager", region_name=aws_region)
    response = client.get_secret_value(SecretId=secret_path)
    return json.loads(response["SecretString"])
