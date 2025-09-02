import asyncio
import logging
import os
import sys

# Completely disable ALL logging to prevent JSON-RPC interference
logging.disable(logging.CRITICAL)
os.environ['ANONYMIZED_TELEMETRY'] = 'false'
os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'CRITICAL'

# Note: stderr redirection removed to avoid interfering with MCP JSON-RPC

from mcp.server.fastmcp import FastMCP
from .agents import run_pool, summarize_bug_reports

# Create FastMCP instance
mcp = FastMCP("vibetest")

@mcp.tool()
async def start(url: str, num_agents: int = 3, headless: bool = False) -> str:
    """Launch browser agents to test a website for UI bugs and issues.
    
    Args:
        url: The website URL to test
        num_agents: Number of QA agents to spawn (default: 3)
        headless: Whether to run browsers in headless mode (default: False)
    
    Returns:
        test_id: Unique identifier for this test run
    """
    try:
        test_id = await run_pool(url, num_agents, headless=headless)
        # Ensure we return a string, not None
        if test_id is None:
            return "Error: No test ID returned from run_pool"
        return str(test_id)
    except Exception as e:
        return f"Error starting test: {str(e)}"

@mcp.tool()
async def results(test_id: str) -> dict:
    """Get the consolidated bug report for a test run.
    
    Args:
        test_id: The test ID returned from start
    
    Returns:
        dict: Complete test results with detailed findings
    """
    try:
        summary = await summarize_bug_reports(test_id)
        
        if "error" in summary:
            return summary
        
        # Get test data to access duration
        from .agents import _test_results
        test_data = _test_results.get(test_id, {})
        
        # Add duration to the summary
        duration_seconds = test_data.get('duration', 0)
        if duration_seconds > 0:
            summary['duration_seconds'] = duration_seconds
            if duration_seconds < 60:
                summary['duration_formatted'] = f"{duration_seconds:.0f}s"
            else:
                minutes = int(duration_seconds // 60)
                seconds = int(duration_seconds % 60)
                summary['duration_formatted'] = f"{minutes}m {seconds}s"
        else:
            summary['duration_formatted'] = "unknown"
        
        return summary
        
    except Exception as e:
        return {"error": f"Error getting results: {str(e)}"}

def run():
    """Entry point for the MCP server"""
    try:
        mcp.run()
        return 0
    except Exception as e:
        return 1

if __name__ == "__main__":
    run()
