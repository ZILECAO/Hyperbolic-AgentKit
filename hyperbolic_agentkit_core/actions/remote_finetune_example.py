import requests
import json
import os
import subprocess
from typing import Optional, Dict, Any
from collections.abc import Callable
from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from langchain.callbacks.manager import CallbackManagerForToolRun

from hyperbolic_agentkit_core.actions.hyperbolic_action import HyperbolicAction

from hyperbolic_agentkit_core.actions.ssh_manager import ssh_manager
from hyperbolic_agentkit_core.actions.get_gpu_status import get_gpu_status


class RunFinetuneInput(BaseModel):
    """Input argument schema for fine-tuning action."""
    model_name: str = Field(
        ..., 
        description="The name of the base model to fine-tune"
    )

def run_finetune(model_name: str) -> str:
    """Run the example finetune action workflow.
    
    Args:
        model_name (str): The name of the base model to fine-tune
        
    Returns:
        str: JSON string containing the status and results of the fine-tuning process
    """
    try:
        # Step 0: Get GPU status
        gpu_status = get_gpu_status()
        if not gpu_status.get("instances"):
            return json.dumps({"status": "error", "message": "No active GPU instances found"})
        
        instance = gpu_status["instances"][0]
        ssh_command = instance["sshCommand"]
        
        # Parse SSH command
        parts = ssh_command.split()
        host = parts[1].split('@')[1]
        port = int(parts[3])
        username = parts[1].split('@')[0]
        
        # Step 1: Establish SSH connection first
        ssh_result = ssh_manager.connect(host=host, port=port, username=username)
        if isinstance(ssh_result, str) and "Error" in ssh_result:
            return json.dumps({"status": "error", "message": f"Failed to connect to remote GPU: {ssh_result}"})
        
        # Step 2: Setup remote environment
        shell_result = ssh_manager.execute(
            "sudo apt-get update && "
            "sudo apt-get install -y rsync python3-dev python3-pip build-essential git cmake pkg-config nano"
        )
        if isinstance(shell_result, str) and ("E: Unable to locate package" in shell_result or 
                                                "E: Failed to fetch" in shell_result or
                                                "E: Could not install" in shell_result):
            return json.dumps({"status": "error", "message": f"Failed to install required packages: {shell_result}"})
        
        # Step 3: Sync files to remote GPU
        sync_result = sync_to_remote()
        if isinstance(sync_result, dict) and not sync_result.get("success", False):
            return json.dumps({"status": "error", "message": sync_result.get("error", "Sync failed")})

        # Step 4: Execute setup, training, and test inference in a single shell session
        combined_command = (
            "cd finetune_example && "
            "bash -c '"
            "python3 -m venv venv && "
            "source venv/bin/activate && "
            "pip install -r requirements.txt && "
            f"FINE_TUNE_MODEL={model_name} python3 finetune.py && "
            "python3 test_inference.py 'What can you tell me about LLMs?'"
            "'"
        )
        
        shell_result = ssh_manager.execute(combined_command)
        
        # Check for the finetuned_model directory
        verify_result = ssh_manager.execute("test -d /home/ubuntu/finetune_example/finetuned_model && echo 'exists'")
        if "exists" not in str(verify_result):
            return json.dumps({
                "status": "error",
                "message": f"Fine-tuning failed or directory not created. Output: {shell_result}"
            })
        
        # Get the inference output
        inference_output = ssh_manager.execute("cat /home/ubuntu/finetune_example/inference_output.json")
        try:
            inference_result = json.loads(inference_output)
        except:
            inference_result = {"error": "Failed to parse inference output"}

        return json.dumps({
            "status": "success",
            "message": "Fine-tuning completed successfully",
            "model_name": model_name,
            "test_inference_output": inference_result
        })

    except Exception as e:
        return json.dumps({
            "status": "error", 
            "message": str(e)
        })

class RunFinetuneAction(HyperbolicAction):
    """Run the example finetune action workflow."""

    name: str = "run_finetune"
    description: str = """This tool will execute fine-tuning of an AI model on Hyperbolic's GPU infrastructure.
    It takes the model name as input (e.g., "unsloth/mistral-7b-v0.3-bnb-4bit").
    The model will be fine-tuned using unsloth and set up for local inference using vLLM.
    Training data should be prepared in advance in the data/training_data.jsonl file."""
    args_schema: type[BaseModel] = RunFinetuneInput
    return_direct: bool = False
    func: Callable[..., str] = run_finetune



def sync_to_remote() -> Dict[str, bool]:
    """Syncs local files to remote GPU for fine-tuning."""
    try:
        # Get GPU status and SSH details
        gpu_status = get_gpu_status()
        if not gpu_status.get("instances"):
            return {"success": False, "error": "No active GPU instances found"}
            
        instance = gpu_status["instances"][0]
        ssh_command = instance["sshCommand"]
        
        # Parse SSH command with error handling
        try:
            parts = ssh_command.split()
            host = parts[1].split('@')[1]
            port = parts[3]
            username = parts[1].split('@')[0]
        except (IndexError, AttributeError) as e:
            return {"success": False, "error": f"Failed to parse SSH command: {str(e)}"}

        # Required files to sync
        local_files = [
            "./finetune_example/training_data.jsonl",
            "./finetune_example/requirements.txt",
            "./finetune_example/finetune.py",
            "./finetune_example/test_inference.py"
        ]
        

        # Check if all required files exist
        for local_file in local_files:
            if not os.path.exists(local_file):
                return {
                    "success": False, 
                    "error": f"Required file not found: {local_file}"
                }

        # Create base directory on remote
        result = ssh_manager.execute("mkdir -p ~/finetune_example")
        if isinstance(result, str) and "error" in result.lower():
            return {
                "success": False, 
                "error": f"Failed to create base directory: {result}"
            }

        
        # Sync each file with error handling
        for local_file in local_files:
            remote_path = f"/home/ubuntu/finetune_example/{'/'.join(local_file.split('/')[2:])}"
            
            # Run rsync with output capture
            result = subprocess.run([
                "rsync", "-avz",
                "-e", f"ssh -p {port}",
                local_file,
                f"{username}@{host}:{remote_path}"
            ], capture_output=True, text=True)

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Failed to sync {local_file}: {result.stderr}"
                }

        # Verify files were synced correctly
        for local_file in local_files:
            remote_path = f"/home/ubuntu/finetune_example/{'/'.join(local_file.split('/')[2:])}"
            check_result = ssh_manager.execute(f"test -f {remote_path} && echo 'exists'")
            
            if "exists" not in str(check_result):
                return {
                    "success": False,
                    "error": f"Failed to verify file sync for: {remote_path}"
                }

        return {
            "success": True,
            "message": "All files synced successfully"
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error during file sync: {str(e)}"
        }