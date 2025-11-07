"""
Encryption utilities for secure origin verification
"""
import base64
from cryptography.fernet import Fernet
from app.config import settings
import logging

logger = logging.getLogger(__name__)

def get_encryption_key():
    """Get encryption key from settings"""
    try:
        # Use the private key from env vars
        private_key = getattr(settings, 'private_key_encrypter', None)
        if private_key:
            # Generate a Fernet key from the private key
            key = base64.urlsafe_b64encode(private_key.encode()[:32].ljust(32, b'\0'))
            return key
        else:
            logger.warning("No encryption key found in settings")
            return None
    except Exception as e:
        logger.error(f"Error getting encryption key: {e}")
        return None

def decrypt_origin(encrypted_origin: str) -> str:
    """
    Decrypt the encrypted origin sent from frontend
    
    Args:
        encrypted_origin: Base64 encoded encrypted origin
        
    Returns:
        Decrypted origin string or None if decryption fails
    """
    try:
        key = get_encryption_key()
        if not key:
            return None
            
        fernet = Fernet(key)
        
        # Decode from base64 and decrypt
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_origin.encode())
        decrypted_bytes = fernet.decrypt(encrypted_bytes)
        decrypted_origin = decrypted_bytes.decode()
        
        logger.info(f"✅ Successfully decrypted origin: {decrypted_origin}")
        return decrypted_origin
        
    except Exception as e:
        logger.error(f"❌ Error decrypting origin: {e}")
        return None

def encrypt_origin(origin: str) -> str:
    """
    Encrypt origin for testing purposes
    
    Args:
        origin: Plain text origin (e.g., "warocol.com")
        
    Returns:
        Base64 encoded encrypted origin
    """
    try:
        key = get_encryption_key()
        if not key:
            return None
            
        fernet = Fernet(key)
        
        # Encrypt and encode to base64
        encrypted_bytes = fernet.encrypt(origin.encode())
        encrypted_origin = base64.urlsafe_b64encode(encrypted_bytes).decode()
        
        return encrypted_origin
        
    except Exception as e:
        logger.error(f"❌ Error encrypting origin: {e}")
        return None