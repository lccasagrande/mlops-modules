# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import Any, List, Optional, cast

from aws_cdk import Aspects, Duration, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from cdk_nag import AwsSolutionsChecks, NagPackSuppression, NagSuppressions
from constructs import Construct, IConstruct


class MlflowFargateStack(Stack):  # type: ignore
    def __init__(
        self,
        scope: Construct,
        id: str,
        app_prefix: str,
        vpc_id: str,
        subnet_ids: List[str],
        ecs_cluster_name: Optional[str],
        service_name: Optional[str],
        ecr_repo_name: str,
        task_cpu_units: int,
        task_memory_limit_mb: int,
        artifacts_bucket_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        Tags.of(scope=cast(IConstruct, self)).add(key="Deployment", value=app_prefix[:64])

        role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal(service="ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonECS_FullAccess"),
            ],
        )

        # Grant artifacts bucket read-write permissions
        model_bucket = s3.Bucket.from_bucket_name(self, "ArtifactsBucket", bucket_name=artifacts_bucket_name)
        model_bucket.grant_read_write(role)

        vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=vpc_id)
        subnets = [ec2.Subnet.from_subnet_id(self, f"Sub{subnet_id}", subnet_id) for subnet_id in subnet_ids]

        cluster = ecs.Cluster(
            self,
            "EcsCluster",
            cluster_name=ecs_cluster_name,
            vpc=vpc,
            container_insights=True,
        )
        self.cluster = cluster

        task_definition = ecs.FargateTaskDefinition(
            self,
            "MlflowTask",
            task_role=role,
            cpu=task_cpu_units,
            memory_limit_mib=task_memory_limit_mb,
        )

        container = task_definition.add_container(
            "ContainerDef",
            # TODO: add ability to pull specific tag
            image=ecs.ContainerImage.from_ecr_repository(
                repository=ecr.Repository.from_repository_name(
                    self,
                    "ECRRepo",
                    repository_name=ecr_repo_name,
                ),
            ),
            environment={
                "BUCKET": f"s3://{artifacts_bucket_name}",
                # TODO: Add persistence
                # "HOST": database.db_instance_endpoint_address,
                # "PORT": str(port),
                # "DATABASE": db_name,
                # "USERNAME": username,
            },
            # secrets={"PASSWORD": ecs.Secret.from_secrets_manager(db_password_secret)},
            logging=ecs.LogDriver.aws_logs(stream_prefix="mlflow"),
        )
        port_mapping = ecs.PortMapping(container_port=5000, host_port=5000, protocol=ecs.Protocol.TCP)
        container.add_port_mappings(port_mapping)

        service = ecs_patterns.NetworkLoadBalancedFargateService(
            self,
            "MlflowLBService",
            service_name=service_name,
            cluster=cluster,
            task_definition=task_definition,
            task_subnets=ec2.SubnetSelection(subnets=subnets),
        )
        lb_access_logs_bucket = s3.Bucket(
            self,
            "LBAccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        service.load_balancer.log_access_logs(bucket=lb_access_logs_bucket)
        self.lb_access_logs_bucket = lb_access_logs_bucket

        # Setup security group
        service.service.connections.security_groups[0].add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(5000),
            description="Allow inbound from VPC for mlflow",
        )

        # Setup autoscaling policy
        scaling = service.service.auto_scale_task_count(max_capacity=2)
        scaling.scale_on_cpu_utilization(
            id="AutoscalingPolicy",
            target_utilization_percent=70,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )
        self.service = service

        # Add CDK nag solutions checks
        Aspects.of(self).add(AwsSolutionsChecks())

        NagSuppressions.add_stack_suppressions(
            self,
            apply_to_nested_stacks=True,
            suppressions=[
                NagPackSuppression(
                    **{
                        "id": "AwsSolutions-IAM4",
                        "reason": "Managed Policies are for src account roles only",
                    }
                ),
                NagPackSuppression(
                    **{
                        "id": "AwsSolutions-IAM5",
                        "reason": "Resource access restricted to resources",
                    }
                ),
                NagPackSuppression(
                    **{
                        "id": "AwsSolutions-ECS2",
                        "reason": "Not passing secrets via env variables",
                    }
                ),
                NagPackSuppression(
                    **{
                        "id": "AwsSolutions-S1",
                        "reason": "Access logs not required for access logs bucket",
                    }
                ),
            ],
        )
