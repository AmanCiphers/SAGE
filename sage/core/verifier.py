class Verifier:
    def verify(self, result):
        print("[VERIFIER] Verifying result...")

        if result is None:
            print("[VERIFIER] Result is empty.")
            return False

        if not isinstance(result, str):
            print("[VERIFIER] Unexpected result type.")
            return False

        if not result.strip():
            print("[VERIFIER] Result is empty.")
            return False

        print("[VERIFIER] Result looks valid.")
        return True
