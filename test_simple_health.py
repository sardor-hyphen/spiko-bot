#!/usr/bin/env python3
"""
Simple test script to verify basic health endpoints work
"""
import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from bot.main import app
import httpx

async def test_simple_endpoints():
    """Test simple health endpoints that don't depend on database"""
    print("Testing simple health endpoints...")
    
    # Test ping endpoint
    print("1. Testing /ping endpoint...")
    try:
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.get("/ping")
            print(f"   HTTP Status: {response.status_code}")
            if response.status_code == 200:
                ping_data = response.json()
                print(f"   Ping response: {ping_data}")
                print("   ✓ PASS")
            else:
                print("   ✗ FAIL - Non-200 status code")
    except Exception as e:
        print(f"   ✗ FAIL - Exception: {e}")
    
    # Test health-lite endpoint
    print("2. Testing /health-lite endpoint...")
    try:
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.get("/health-lite")
            print(f"   HTTP Status: {response.status_code}")
            if response.status_code == 200:
                health_data = response.json()
                print(f"   Health-lite response: {health_data}")
                print("   ✓ PASS")
            else:
                print("   ✗ FAIL - Non-200 status code")
    except Exception as e:
        print(f"   ✗ FAIL - Exception: {e}")
    
# Test regular health endpoint (may be degraded)
    print("3. Testing /health endpoint...")
    try:
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.get("/health")
            print(f"   HTTP Status: {response.status_code}")
            if response.status_code == 200:
                health_data = response.json()
                print(f"   Health response: {health_data}")
                print("   ✓ PASS")
            else:
                print("   ✗ FAIL - Non-200 status code")
    except Exception as e:
        print(f"   ✗ FAIL - Exception: {e}")

    # Test cron-health endpoint
    print("4. Testing /cron-health endpoint...")
    try:
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.get("/cron-health")
            print(f"   HTTP Status: {response.status_code}")
            if response.status_code == 200:
                cron_data = response.json()
                print(f"   Cron-health response: {cron_data}")
                print("   ✓ PASS")
            else:
                print("   ✗ FAIL - Non-200 status code")
    except Exception as e:
        print(f"   ✗ FAIL - Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_simple_endpoints())