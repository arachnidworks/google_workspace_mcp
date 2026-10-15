"""AW entrypoint: apply the AW overlay, then run upstream main.py unchanged."""

import os

from dotenv import load_dotenv

# main.py loads .env before importing core.server; the overlay imports it first.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import aw.overlay  # noqa: E402,F401  patches core.server before main.py binds it
import main  # noqa: E402

main.SERVICE_MODULES["chat"] = "aw.chat_tools"

if __name__ == "__main__":
    main.main()
