#!/usr/bin/env python3
"""
Simple test script to verify the health check endpoint
"""
import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from bot.main import app
from bot.db import check_db_health
import httpx

async def test_health_endpoint():
    """Test the health check endpoint"""
    print("Testing health check endpoint...")
    
    # Test database health check directly
    print("1. Testing database health check...")
    db_health = await check_db_health()
    print(f"   Database health: {'✓ PASS' if db_health else '✗ FAIL'}")
    
    # Test health endpoint via HTTP
    print("2. Testing health endpoint via HTTP...")
    try:
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.get("/health")
            print(f"   HTTP Status: {response.status_code}")
            if response.status_code == 200:
                health_data = response.json()
                print(f"   Health data: {health_data}")
                print("   ✓ PASS")
            else:
                print("   ✗ FAIL - Non-200 status code")
    except Exception as e:
        print(f"   ✗ FAIL - Exception: {e}")
    
    # Test root endpoint
    print("3. Testing root endpoint...")
    try:
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.get("/")
            print(f"   HTTP Status: {response.status_code}")
            if response.status_code == 200:
                root_data = response.json()
                print(f"   Root data: {root_data}")
                print("   ✓ PASS")
            else:
                print("   ✗ FAIL - Non-200 status code")
    except Exception as e:
        print(f"   ✗ FAIL - Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_health_endpoint())