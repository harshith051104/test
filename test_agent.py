# 🤖 Agent Call Test Script

## Test Google ADK Integration

import sys
import os
sys.path.append('.')

def test_agent_call():
    """Test the agent_call.py functionality"""
    try:
        print("🧪 Testing Agent Call Integration...")
        print("=" * 50)
        
        # Test imports
        print("1. Testing imports...")
        import agent_call
        print("✅ agent_call.py imported successfully")
        
        # Test ADK Agent initialization
        print("\n2. Testing ADK Agent...")
        adk_agent = agent_call.GoogleADKAgent("test-project", "us-central1")
        print("✅ GoogleADKAgent initialized")
        
        # Test session creation
        print("\n3. Testing session creation...")
        session = adk_agent.create_session("test_call_123")
        print(f"✅ Session created: {session}")
        
        # Test mock responses (without actual Google Cloud setup)
        print("\n4. Testing mock responses...")
        
        # Test STT (will use mock)
        mock_audio = b'\x00' * 100
        transcript = adk_agent.speech_to_text(mock_audio)
        print(f"✅ STT Test: {transcript}")
        
        # Test Dialogflow (will use mock)
        response = adk_agent.process_with_dialogflow("Hello", session)
        print(f"✅ Dialogflow Test: {response}")
        
        # Test TTS (will use mock)
        audio = adk_agent.text_to_speech("Hello, this is a test")
        print(f"✅ TTS Test: Generated {len(audio)} bytes of audio")
        
        print("\n" + "=" * 50)
        print("🎉 Agent Call Integration Test PASSED!")
        print("\nNext Steps:")
        print("1. Set up Google Cloud credentials")
        print("2. Create Dialogflow agent")
        print("3. Configure environment variables")
        print("4. Start the agent: python agent_call.py")
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False

if __name__ == "__main__":
    test_agent_call()
