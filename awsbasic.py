import boto3
import os
import random
import string
import time
import base64
from dotenv import load_dotenv
from botocore.exceptions import ClientError

# Load environment variables from .env file
load_dotenv()

# Function to generate random names
def generate_random_name(prefix, length=8):
    """Generate a random name with the given prefix and length."""
    return prefix + ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

# Initialize AWS session
session = boto3.Session(
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    region_name=os.getenv('AWS_REGION')
)

# Create AWS clients
ec2 = session.client('ec2')

def create_resources():
    try:
        # Create VPC
        vpc_response = ec2.create_vpc(CidrBlock='10.0.0.0/16')
        vpc_id = vpc_response['Vpc']['VpcId']
        print(f'Created VPC: {vpc_id}')
        time.sleep(5)  # Wait for VPC to be fully available

        # Enable DNS support and hostnames for the main VPC
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={'Value': True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={'Value': True})

        # Create Internet Gateway
        igw_response = ec2.create_internet_gateway()
        igw_id = igw_response['InternetGateway']['InternetGatewayId']
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        print(f'Created and attached Internet Gateway: {igw_id}')
        time.sleep(5)  # Wait for IGW to be attached

        # Create subnets
        subnet_ids = []
        availability_zones = ec2.describe_availability_zones()['AvailabilityZones']
        for i, az in enumerate(availability_zones[:2]):
            subnet_response = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=f'10.0.{i}.0/24',
                AvailabilityZone=az['ZoneName']
            )
            subnet_id = subnet_response['Subnet']['SubnetId']
            subnet_ids.append(subnet_id)
            print(f'Created Subnet: {subnet_id} in {az["ZoneName"]}')
            time.sleep(2)  # Wait for subnet to be fully available

        # Create route table
        route_table_response = ec2.create_route_table(VpcId=vpc_id)
        if 'RouteTable' in route_table_response:
            route_table_id = route_table_response['RouteTable']['RouteTableId']
            ec2.create_route(RouteTableId=route_table_id, DestinationCidrBlock='0.0.0.0/0', GatewayId=igw_id)
            for subnet_id in subnet_ids:
                ec2.associate_route_table(RouteTableId=route_table_id, SubnetId=subnet_id)
            print(f'Created and associated Route Table: {route_table_id}')
        else:
            print("Failed to create route table.")
            return

        time.sleep(5)  # Ensure route table is fully associated

        # Create security group
        security_group_id = create_security_group(vpc_id)

        # Launch main instance
        main_instance_id = launch_main_instance(security_group_id, subnet_ids[0])
        if main_instance_id:
            optimize_networking(main_instance_id)
            update_env_with_public_ip(main_instance_id)

    except ClientError as e:
        print(f"Error creating resources: {e}")
        # Implement retry logic or alternative actions if needed
        time.sleep(5)  # Ensure route table is fully associated

        # Create security group
        security_group_id = create_security_group(vpc_id)

        # Launch main instance
        main_instance_id = launch_main_instance(security_group_id, subnet_ids[0])
        if main_instance_id:
            optimize_networking(main_instance_id)
            update_env_with_public_ip(main_instance_id)

    except ClientError as e:
        print(f"Error creating resources: {e}")
        # Implement retry logic or alternative actions if needed

def create_security_group(vpc_id):
    try:
        response = ec2.create_security_group(
            GroupName='RDPAccessSecurityGroup',
            Description='Security group for RDP access',
            VpcId=vpc_id
        )
        security_group_id = response['GroupId']
        print(f'Created Security Group: {security_group_id}')
        ec2.authorize_security_group_ingress(
            GroupId=security_group_id,
            IpPermissions=[
                {'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                {'IpProtocol': 'tcp', 'FromPort': 443, 'ToPort': 443, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                {'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22, 'IpRanges': [{'CidrIp': f'{os.getenv("YOUR_IP_ADDRESS")}/32'}]},
                {'IpProtocol': 'tcp', 'FromPort': 3389, 'ToPort': 3389, 'IpRanges': [{'CidrIp': f'{os.getenv("YOUR_IP_ADDRESS")}/32'}]},
                {'IpProtocol': 'tcp', 'FromPort': 9001, 'ToPort': 9001, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},  # ORPort for Tor
                {'IpProtocol': 'tcp', 'FromPort': 9030, 'ToPort': 9030, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}   # DirPort for Tor
            ]
        )
        return security_group_id
    except ClientError as e:
        print(f"Error creating security group: {e}")
        return None

def launch_main_instance(security_group_id, subnet_id):
    try:
        instance_response = ec2.run_instances(
            ImageId=os.getenv('AMI_ID'),  # Fetch AMI ID from .env
            InstanceType=os.getenv('INSTANCE_TYPE', 't3.micro'),  # Fetch instance type from .env
            KeyName=os.getenv('KEY_PAIR_NAME'),
            NetworkInterfaces=[{
                'AssociatePublicIpAddress': True,
                'DeviceIndex': 0,
                'SubnetId': subnet_id,
                'Groups': [security_group_id]
            }],
            MinCount=1,
            MaxCount=1,
            UserData=get_user_data()
        )
        instance_id = instance_response['Instances'][0]['InstanceId']
        print(f'Launched main instance with ID: {instance_id}')
        waiter = ec2.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id])
        return instance_id
    except ClientError as e:
        print(f"Error launching main instance: {e}")
        return None

def optimize_networking(instance_id):
    try:
        print(f"Stopping instance {instance_id} for enhanced networking setup.")
        ec2.stop_instances(InstanceIds=[instance_id])
        waiter = ec2.get_waiter('instance_stopped')
        waiter.wait(InstanceIds=[instance_id])
        time.sleep(5)  # Wait for instance to be fully stopped

        ec2.modify_instance_attribute(
            InstanceId=instance_id,
            EnaSupport={'Value': True}
        )
        print(f"Enhanced networking enabled for instance {instance_id}.")

        ec2.start_instances(InstanceIds=[instance_id])
        waiter = ec2.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id])
        print(f"Instance {instance_id} restarted after networking optimization.")
    except ClientError as e:
        print(f"Error optimizing networking: {e}")

def update_env_file(vpc_id, subnet_ids):
    try:
        with open('.env', 'r') as file:
            env_data = file.readlines()
        with open('.env', 'w') as file:
            for line in env_data:
                if line.startswith('VPC_ID='):
                    file.write(f'VPC_ID={vpc_id}\n')
                elif line.startswith('SUBNET_IDS='):
                    file.write(f'SUBNET_IDS={",".join(subnet_ids)}\n')
                else:
                    file.write(line)
        print("Updated .env with new VPC and subnet IDs.")
    except Exception as e:
        print(f"Error updating .env file: {e}")

def update_env_with_public_ip(instance_id):
    try:
        instance_description = ec2.describe_instances(InstanceIds=[instance_id])
        public_ip = instance_description['Reservations'][0]['Instances'][0]['PublicIpAddress']
        with open('.env', 'r') as file:
            env_data = file.readlines()
        with open('.env', 'w') as file:
            for line in env_data:
                if line.startswith('EC2_PUBLIC_IP='):
                    file.write(f'EC2_PUBLIC_IP={public_ip}\n')
                else:
                    file.write(line)
        print(f"Updated .env with EC2 public IP: {public_ip}")
    except Exception as e:
        print(f"Error updating .env with public IP: {e}")

# Placeholder function for user data script
def get_user_data():
    # This function should return the base64-encoded user data script
    # Ensure the user data is correctly base64-encoded
    user_data_script = '''#!/bin/bash
    # Your initialization script here
    echo "Initializing instance..."
    '''
    return base64.b64encode(user_data_script.encode('utf-8')).decode('utf-8')

# Main execution
create_resources()