import subprocess


class Hermes:
    def run(self, task, model=None):
        print("[HERMES] Preparing command...")

        command = ["hermes"]

        if model:
            command.extend(["-m", model])

        command.extend(["-z", task])

        print(f"[HERMES] Model: {model}")
        print(f"[HERMES] Task: {task}")
        print("[HERMES] Starting Hermes process...")

        result = subprocess.run(
            command,
            capture_output=True,
            text=True
        )

        print(f"[HERMES] Process exited with code: {result.returncode}")

        if result.returncode != 0:
            print(f"[HERMES] STDERR:\n{result.stderr}")

            raise RuntimeError(
                f"Hermes failed:\n{result.stderr}"
            )

        print("[HERMES] Received response.")

        return result.stdout.strip()
