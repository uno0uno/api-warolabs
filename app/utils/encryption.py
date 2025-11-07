"""
Simple encryption utilities for secure origin verification
"""
import base64
import hashlib
from datetime import datetime
from app.config import settings
import logging

logger = logging.getLogger(__name__)

def get_simple_key():
    """Get simple encryption key from settings"""
    try:
        # Use a part of the JWT secret as encryption key (simpler approach)
        jwt_secret = getattr(settings, 'jwt_secret', None)
        if jwt_secret:
            # Create a simple key from JWT secret
            return jwt_secret[:32].encode().ljust(32, b'0')
        else:
            logger.warning("No JWT secret found for simple encryption")
            return None
    except Exception as e:
        logger.error(f"Error getting simple encryption key: {e}")
        return None

def decrypt_origin(encrypted_origin: str) -> str:
    """
    Decrypt the simple encrypted origin sent from frontend
    
    Args:
        encrypted_origin: Base64 encoded encrypted origin
        
    Returns:
        Decrypted origin string or None if decryption fails
    """
    try:
        key = get_simple_key()
        if not key:
            return None
        
        # Simple decode from base64 (match frontend btoa)
        try:
            decoded = base64.b64decode(encrypted_origin.encode()).decode()
            
            # Parse the payload: origin|timestamp|key_part
            parts = decoded.split('|')
            if len(parts) >= 3:
                origin = parts[0]
                timestamp = parts[1]
                sent_key_part = parts[2]
                
                # Verify key part matches
                expected_key_part = key[:8].decode('utf-8', errors='ignore')
                if sent_key_part == expected_key_part:
                    logger.info(f"✅ Successfully decrypted origin: {origin}")
                    return origin
                else:
                    logger.warning(f"❌ Key verification failed")
                    return None
            else:
                logger.warning(f"❌ Invalid encrypted payload format")
                return None
                
        except Exception as e:
            logger.warning(f"❌ Error parsing encrypted origin: {e}")
            return None
        
    except Exception as e:
        logger.error(f"❌ Error decrypting origin: {e}")
        return None

def encrypt_origin(origin: str) -> str:
    """
    Encrypt origin for testing purposes (matches frontend implementation)
    
    Args:
        origin: Plain text origin (e.g., "warocol.com")
        
    Returns:
        Base64 encoded encrypted origin
    """
    try:
        key = get_simple_key()
        if not key:
            return None
            
        # Get timestamp and key part (match frontend)
        timestamp = str(int(datetime.now().timestamp() * 1000))  # JavaScript Date.now() format
        key_part = key[:8].decode('utf-8', errors='ignore')
        
        # Create payload: origin|timestamp|key_part
        payload = f"{origin}|{timestamp}|{key_part}"
        
        # Simple base64 encoding (match frontend btoa)
        encoded = base64.b64encode(payload.encode()).decode()
        
        return encoded
        
    except Exception as e:
        logger.error(f"❌ Error encrypting origin: {e}")
        return None