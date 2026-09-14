# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments MCP Server - Entry point for AgentCore Runtime.

This file is the entry point configured in the AgentCore Runtime.
It imports and runs the MCP server from the src package.
"""

import sys
import os

# Ensure the zip root (where this file lives, /var/task) is in the path
# so that 'src' package can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.mcp_server.server import main

if __name__ == "__main__":
    main()
