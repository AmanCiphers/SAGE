import subprocess


class Hermes:
    def run(self, task):
        result = subprocess.run(
            ["hermes", "-z", task],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Hermes failed:\n{result.stderr}"
            )

        return result.stdout.strip()
