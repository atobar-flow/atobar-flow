import json
from typing import Any, Dict, List


def todo_write(todos: List[Dict[str, Any]]) -> str:
    """
    Serializes the agent's cognitive plan to disk.

    Expected format for each todo item:
    {"content": "Description of the task", "status": "pending" | "in_progress" | "completed"}

    Enforces the rule that exactly ONE task can be marked 'in_progress' at a time.
    """
    normalized_todos = []
    in_progress_count = 0
    for task in todos:
        content = task.get("content") or task.get("title", "")
        if not content:
            raise ValueError("Task 'content' cannot be empty.")
        status = task.get("status")
        if status == "done":
            status = "completed"
        if status not in ["pending", "in_progress", "completed"]:
            raise ValueError(f"Invalid status '{status}'. Must be 'pending', 'in_progress', or 'completed'.")
        if status == "in_progress":
            in_progress_count += 1
        normalized_todos.append({"content": content, "status": status})

    if in_progress_count > 1:
        raise ValueError("Keep exactly ONE task in_progress at a time.")

    todos = normalized_todos

    # Optional: We could also enforce at least one in_progress if tasks exist, but the req says "more than ONE".

    state_file = "/home/ubuntu/workspace/.atobar/state.json"
    try:
        import os
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"tasks": todos}, f, indent=2)
    except IOError as e:
        # Fallback to local execution testing when not in the ec2 workspace
        try:
            import os
            os.makedirs(".atobar", exist_ok=True)
            with open(".atobar/state.json", "w", encoding="utf-8") as f:
                json.dump({"tasks": todos}, f, indent=2)
        except IOError as fallback_e:
            raise IOError(f"Failed to write state file: {str(e)}") from fallback_e

    # Attempt to trigger the background WIP syncer immediately if it's running
    from flow_agent.main import trigger_wip_sync
    trigger_wip_sync()

    return "Successfully updated cognitive plan."
