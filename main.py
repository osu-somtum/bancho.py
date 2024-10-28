#!/usr/bin/env python3.11
"""main.py - a user-friendly, safe wrapper around bancho.py's runtime

bancho.py is an in-progress osu! server implementation for developers of all levels
of experience interested in hosting their own osu private server instance(s).

the project is developed primarily by the Akatsuki (https://akatsuki.pw) team,
and our aim is to create the most easily maintainable, reliable, and feature-rich
osu! server implementation available.

we're also fully open source!
https://github.com/osuAkatsuki/bancho.py
"""
import os
import argparse
import logging
import sys
from collections.abc import Sequence
import uvicorn
from starlette.middleware.cors import CORSMiddleware
from starlette.applications import Starlette

import app.utils
import app.settings
from app.logging import Ansi
from app.logging import log

def main(argv: Sequence[str]) -> int:
    # Ensure runtime environment is ready
    app.utils.setup_runtime_environment()

    for safety_check in (
        app.utils.ensure_supported_platform,
        app.utils.ensure_directory_structure,
    ):
        exit_code = safety_check()
        if exit_code != 0:
            return exit_code

    # Parse and handle command-line arguments
    parser = argparse.ArgumentParser(
        description=("An open-source osu! server implementation by Akatsuki."),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s v{app.settings.VERSION}",
    )
    parser.parse_args(argv)

    # Install any debugging hooks
    app.utils._install_debugging_hooks()

    # Check internet connection status
    if not app.utils.check_connection(timeout=1.5):
        log("No internet connection available.", Ansi.LYELLOW)

    # Show info and any contextual warnings
    app.utils.display_startup_dialog()

    # Server configuration
    uds = None
    host = None
    port = None

    if (
        app.utils.is_valid_inet_address(app.settings.APP_HOST)
        and app.settings.APP_PORT is not None
    ):
        host = app.settings.APP_HOST
        port = app.settings.APP_PORT
    elif (
        app.utils.is_valid_unix_address(app.settings.APP_HOST)
        and app.settings.APP_PORT is None
    ):
        uds = app.settings.APP_HOST
        if os.path.exists(app.settings.APP_HOST):
            if app.utils.processes_listening_on_unix_socket(app.settings.APP_HOST) != 0:
                log(
                    f"There are other processes listening on {app.settings.APP_HOST}.\n"
                    f"If you've lost it, bancho.py can be killed gracefully with SIGINT.",
                    Ansi.LRED,
                )
                return 1
            else:
                os.remove(app.settings.APP_HOST)
    else:
        raise ValueError(
            "%r does not appear to be an IPv4, IPv6 or Unix address"
            % app.settings.APP_HOST,
        ) from None

    # Create the ASGI app
    asgi_app = Starlette()

    # Apply CORS middleware to allow all origins
    asgi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Run the server
    uvicorn.run(
        "app.api.init_api:asgi_app",  # Pass the ASGI app as an import string
        reload=app.settings.DEBUG,
        log_level=logging.WARNING,
        server_header=False,
        date_header=False,
        headers=[("bancho-version", app.settings.VERSION)],
        uds=uds,
        host=host or "127.0.0.1",
        port=port or 8000,
    )

    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
