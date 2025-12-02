"""
Test script for Discord error notifications
Run this to verify that Discord notifications are working
"""
import asyncio
import sys
from pathlib import Path

# Add the app directory to the path
sys.path.insert(0, str(Path(__file__).parent))

from app.services.discord_error_notifier import error_notifier


async def test_error_notification():
    """Test sending an error notification"""
    print("Testing error notification...")

    try:
        # Simulate an error
        raise ValueError("This is a test error from the Discord notification system")
    except Exception as e:
        await error_notifier.send_error(
            error=e,
            context={
                "test_mode": True,
                "component": "Discord Notification Test"
            },
            request_info={
                "method": "TEST",
                "url": "http://localhost:5000/test",
                "client_host": "127.0.0.1",
                "user_agent": "Discord Test Script"
            }
        )

    print("✅ Error notification sent!")


async def test_warning_notification():
    """Test sending a warning notification"""
    print("\nTesting warning notification...")

    await error_notifier.send_warning(
        title="Test Warning",
        message="This is a test warning from the Discord notification system",
        context={
            "test_mode": True,
            "severity": "low"
        }
    )

    print("✅ Warning notification sent!")


async def test_info_notification():
    """Test sending an info notification"""
    print("\nTesting info notification...")

    await error_notifier.send_info(
        title="Test Info",
        message="This is a test info message from the Discord notification system",
        context={
            "test_mode": True,
            "purpose": "System verification"
        }
    )

    print("✅ Info notification sent!")


async def main():
    """Run all tests"""
    print("=" * 60)
    print("Discord Notification System Test")
    print("=" * 60)
    print()

    try:
        await test_error_notification()
        await asyncio.sleep(1)  # Wait a bit between notifications

        await test_warning_notification()
        await asyncio.sleep(1)

        await test_info_notification()

        print()
        print("=" * 60)
        print("✅ All tests completed!")
        print("Check your Discord channel for the notifications")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Test failed: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
