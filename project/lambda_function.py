import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os
import hashlib

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors, create_zip

eh = ExtensionHandler()

codebuild = boto3.client('codebuild')

## REFER TO
## https://docs.aws.amazon.com/codebuild/latest/userguide/available-runtimes.html
## for a list of available runtimes and their matching images

def lambda_handler(event, context):
    try:
        print(f"event = {event}")
        account_number = account_context(context)['number']
        region = account_context(context)['region']
        eh.capture_event(event)

        prev_state = event.get("prev_state") or {}
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")

        name = cdef.get("name") or component_safe_name(
            project_code, repo_id, cname, max_chars=255
        )
        trust_level = cdef.get("trust_level")

        role_arn = lambda_env("codebuild_role_arn")
        
        description = f"Codebuild project for component {cname} in app {repo_id}"
        build_container_size = cdef.get("build_container_size")
        runtime_versions = cdef.get("runtime_versions") or None
        environment_variables = cdef.get("environment_variables") or None
        install_commands = cdef.get("install_commands") or None
        pre_build_commands = cdef.get("pre_build_commands") or None
        build_commands = cdef.get("build_commands") or None
        post_build_commands = cdef.get("post_build_commands") or None
        buildspec_artifacts = cdef.get("buildspec_artifacts") or None

        privileged_mode = cdef.get("privileged_mode") or False

        artifacts = cdef.get("artifacts")

        container_image = cdef.get("container_image") or "aws/codebuild/standard:5.0"
    
        if event.get("pass_back_data"):
            print(f"pass_back_data found")
        elif event.get("op") == "upsert":
            if trust_level == "full":
                eh.add_op("compare_defs")
            else:
                eh.add_op("get_codebuild_project")

        elif event.get("op") == "delete":
            eh.add_op("remove_codebuild_project", {"create_and_remove": False, "name": name})
            
        compare_defs(event)

        if build_container_size:
            if isinstance(build_container_size, str):
                if build_container_size.lower() == "small":
                    build_container_size = "BUILD_GENERAL1_SMALL"
                elif build_container_size.lower() == "medium":
                    build_container_size = "BUILD_GENERAL1_MEDIUM"
                elif build_container_size.lower() == "large":
                    build_container_size = "BUILD_GENERAL1_LARGE"
                elif (build_container_size.lower() == "2xlarge") or (build_container_size.lower() == "xxlarge"):
                    build_container_size = "BUILD_GENERAL1_2XLARGE"
                elif build_container_size in ["BUILD_GENERAL1_SMALL", "BUILD_GENERAL1_MEDIUM", "BUILD_GENERAL1_LARGE", "BUILD_GENERAL1_2XLARGE"]:
                    pass
                else:
                    eh.add_log("Invalid build_container_size, using LARGE", {"build_container_size": build_container_size})
                    build_container_size = "BUILD_GENERAL1_LARGE"
            else:
                if build_container_size == 1:
                    build_container_size = "BUILD_GENERAL1_SMALL"
                elif build_container_size == 2:
                    build_container_size = "BUILD_GENERAL1_MEDIUM"
                elif build_container_size == 3:
                    build_container_size = "BUILD_GENERAL1_LARGE"
                elif build_container_size == 4:
                    build_container_size = "BUILD_GENERAL1_2XLARGE"
                else:
                    eh.add_log("Invalid build_container_size, using LARGE", {"build_container_size": build_container_size})
                    build_container_size = "BUILD_GENERAL1_LARGE"
        else:
            build_container_size = "BUILD_GENERAL1_LARGE"

        sourced_from_s3 = cdef.get("sourced_from_s3", True)
        if sourced_from_s3:
            source = {
                "type": "S3",
                "location": f"{cdef['s3_bucket']}/{cdef['s3_object']}",
                "buildspec": json.dumps(remove_none_attributes({
                    "version": 0.2,
                    "env": remove_none_attributes({
                        "variables": environment_variables
                    }) or None,
                    "phases": remove_none_attributes({
                        "install": remove_none_attributes({
                            "runtime-versions": runtime_versions,
                            "commands": install_commands
                        }) or None,
                        "pre_build": remove_none_attributes({
                            "commands": pre_build_commands
                        }) or None,
                        "build": remove_none_attributes({
                            "commands": build_commands
                        }) or None,
                        "post_build": remove_none_attributes({
                            "commands": post_build_commands
                        }) or None
                    }), 
                    "artifacts": buildspec_artifacts
                }), sort_keys=True)
            }
        else:
            source = cdef.get("source")

        codebuild_spec = {
            "name": name,
            "description": description,
            "source": source,
            "artifacts": artifacts,
            "environment": {
                "type": "LINUX_CONTAINER",
                "image": container_image,
                "computeType": build_container_size,
                "imagePullCredentialsType": "CODEBUILD",
                "privilegedMode": privileged_mode
            },
            "serviceRole": role_arn
        }
        print(f"params = {codebuild_spec}")

        codebuild_spec_hash = hashlib.md5(json.dumps(codebuild_spec, sort_keys=True).encode("utf-8")).hexdigest()

        eh.add_props({
            "buildspec_hash": codebuild_spec_hash
        })

        get_codebuild_project(name, codebuild_spec, prev_state, region, account_number)
        create_codebuild_project(name, codebuild_spec)
        update_codebuild_project(name, codebuild_spec)
        remove_codebuild_project()
            
        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

@ext(handler=eh, op="compare_defs")
def compare_defs(event):
    old_rendef = event.get("prev_state", {}).get("rendef", {})
    new_rendef = event.get("component_def")

    _ = old_rendef.pop("trust_level", None)
    _ = new_rendef.pop("trust_level", None)

    if old_rendef != new_rendef:
        eh.add_op("get_codebuild_project")

    else:
        eh.add_links(event.get("prev_state", {}).get('links'))
        eh.add_props(event.get("prev_state", {}).get('props'))
        eh.add_log("Full Trust, No Change: Exiting", {"old": old_rendef, "new": new_rendef})

@ext(handler=eh, op="get_codebuild_project")
def get_codebuild_project(name, codebuild_spec, prev_state, region, account_number):

    if prev_state and prev_state.get("props") and prev_state.get("props").get("name"):
        prev_name = prev_state.get("props").get("name")
        if name != prev_name:
            eh.add_op("remove_codebuild_project", {"create_and_remove": True, "name": prev_name})

    # arn = gen_codebuild_arn(name, region, account_number)

    try:
        response = codebuild.batch_get_projects(names=[name])
        if response.get("projects"):
            eh.add_log("Found Codebuild Project", response.get("projects")[0])
            project = response.get("projects")[0]
            for k, v in codebuild_spec.items():
                if k == "tags":
                    desired_tags_dict = unformat_tags(v)
                    current_tags_dict = unformat_tags(project.get("tags"))
                    if desired_tags_dict != current_tags_dict:
                        eh.add_log("Tags Don't Match, Updating", {"desired": desired_tags_dict, "current": current_tags_dict})
                        eh.add_op("update_codebuild_project", {"name": name, "tags": v})
                        break
                if project.get(k) != v:
                    eh.add_op("update_codebuild_project")
                    break

            if not eh.ops.get("update_codebuild_project"):
                eh.add_log("Codebuild Project Matches; Exiting", {"project": project, "spec": codebuild_spec})
        else:
            eh.add_op("create_codebuild_project")

    except botocore.exceptions.ClientError as e:
        handle_common_errors(e, eh, "Get Codebuild Project Failed", 10)

@ext(handler=eh, op="create_codebuild_project")
def create_codebuild_project(name, codebuild_spec):

    try:
        response = codebuild.create_project(**codebuild_spec).get("project")
        eh.add_log("Created Codebuild Project", response)
        eh.add_props({
            "arn": response['arn'],
            "name": response['name']
        })
        eh.add_links({"Codebuild Project": gen_codebuild_link(name)})
    except ClientError as e:
        handle_common_errors(
            e, eh, "Create Codebuild Project Failed", 20,
            perm_errors=["InvalidInputException", "AccountLimitExceededException"]
        )

@ext(handler=eh, op="update_codebuild_project")
def update_codebuild_project(name, codebuild_spec):
    try:
        response = codebuild.update_project(**codebuild_spec).get("project")
        eh.add_log("Updated Codebuild Project", response)
        eh.add_props({
            "arn": response['arn'],
            "name": response['name']
        })
        eh.add_links({"Codebuild Project": gen_codebuild_link(name)})
    except ClientError as e:
        handle_common_errors(
            e, eh, "Update Codebuild Project Failed", 20,
            perm_errors=["InvalidInputException", "ResourceNotFoundException"]
        )

@ext(handler=eh, op="remove_codebuild_project")
def remove_codebuild_project():
    codebuild_project_name = eh.ops['remove_codebuild_project'].get("name")
    car = eh.ops['remove_codebuild_project'].get("create_and_remove")

    try:
        _ = codebuild.delete_project(name=codebuild_project_name)
        eh.add_log("Deleted Project if it Existed", {"name": codebuild_project_name})
    except botocore.exceptions.ClientError as e:
        eh.add_log("Remove Codebuild Error", {"error": str(e)}, True)
        eh.retry_error(str(e), 60 if car else 15)


def format_tags(tags_dict):
    return [{"Key": k, "Value": v} for k,v in tags_dict]

def unformat_tags(tags_list):
    return {t["Key"]: t["Value"] for t in tags_list}

def gen_codebuild_arn(codebuild_project_name, region, account_number):
    return f"arn:aws:codebuild:{region}:{account_number}:project/{codebuild_project_name}"

def gen_codebuild_link(codebuild_project_name):
    return f"https://console.aws.amazon.com/codesuite/codebuild/projects/{codebuild_project_name}"


"""
aws/codebuild/amazonlinux2-x86_64-standard:3.0	
AMAZON LINUX 2 AVAILABILITY:
version: 0.1

runtimes:
  android:
    versions:
      28:
        requires:
          java: ["corretto8"]
        commands:
          - echo "Installing Android version 28 ..."
      29:
        requires:
          java: ["corretto8"]
        commands:
          - echo "Installing Android version 29 ..."
  java:
    versions:
      corretto11:
        commands:
          - echo "Installing corretto(OpenJDK) version 11 ..."

          - export JAVA_HOME="$JAVA_11_HOME"

          - export JRE_HOME="$JRE_11_HOME"

          - export JDK_HOME="$JDK_11_HOME"

          - |-
            for tool_path in "$JAVA_HOME"/bin/*;
             do tool=`basename "$tool_path"`;
              if [ $tool != 'java-rmi.cgi' ];
              then
               rm -f /usr/bin/$tool /var/lib/alternatives/$tool \
                && update-alternatives --install /usr/bin/$tool $tool $tool_path 20000;
              fi;
            done
      corretto8:
        commands:
          - echo "Installing corretto(OpenJDK) version 8 ..."

          - export JAVA_HOME="$JAVA_8_HOME"

          - export JRE_HOME="$JRE_8_HOME"

          - export JDK_HOME="$JDK_8_HOME"

          - |-
            for tool_path in "$JAVA_8_HOME"/bin/* "$JRE_8_HOME"/bin/*;
             do tool=`basename "$tool_path"`;
              if [ $tool != 'java-rmi.cgi' ];
              then
               rm -f /usr/bin/$tool /var/lib/alternatives/$tool \
                && update-alternatives --install /usr/bin/$tool $tool $tool_path 20000;
              fi;
            done
  golang:
    versions:
      1.12:
        commands:
          - echo "Installing Go version 1.12 ..."
          - goenv global  $GOLANG_12_VERSION
      1.13:
        commands:
          - echo "Installing Go version 1.13 ..."
          - goenv global  $GOLANG_13_VERSION
      1.14:
        commands:
          - echo "Installing Go version 1.14 ..."
          - goenv global  $GOLANG_14_VERSION
  python:
    versions:
      3.9:
        commands:
          - echo "Installing Python version 3.9 ..."
          - pyenv global  $PYTHON_39_VERSION
      3.8:
        commands:
          - echo "Installing Python version 3.8 ..."
          - pyenv global  $PYTHON_38_VERSION
      3.7:
        commands:
          - echo "Installing Python version 3.7 ..."
          - pyenv global  $PYTHON_37_VERSION
  php:
    versions:
      7.4:
        commands:
          - echo "Installing PHP version 7.4 ..."
          - phpenv global $PHP_74_VERSION
      7.3:
        commands:
          - echo "Installing PHP version 7.3 ..."
          - phpenv global $PHP_73_VERSION
  ruby:
    versions:
      2.6:
        commands:
          - echo "Installing Ruby version 2.6 ..."
          - rbenv global $RUBY_26_VERSION
      2.7:
        commands:
          - echo "Installing Ruby version 2.7 ..."
          - rbenv global $RUBY_27_VERSION
  nodejs:
    versions:
      10:
        commands:
          - echo "Installing Node.js version 10 ..."
          - n $NODE_10_VERSION
      12:
        commands:
          - echo "Installing Node.js version 12 ..."
          - n $NODE_12_VERSION
  docker:
    versions:
      18:
        commands:
          - echo "Using Docker 19"
      19:
        commands:
          - echo "Using Docker 19"
  dotnet:
    versions:
      3.1:
        commands:
          - echo "Installing .NET version 3.1 ..."
"""

    

