import base64
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

def sign_message(private_key_path, message):
    with open(private_key_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(key_file.read(), password=None)
    
    signature = private_key.sign(
        message.encode('utf-8'),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def get_auth_headers(api_key_id, private_key_path, method, path):
    timestamp = str(int(time.time() * 1000))
    msg_to_sign = timestamp + method + path
    signature = sign_message(private_key_path, msg_to_sign)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp
    }
