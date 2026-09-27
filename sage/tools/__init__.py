from dotenv import load_dotenv

# Tool modules read configuration directly, so they cannot rely on some other
# import having populated the environment first.
load_dotenv()
