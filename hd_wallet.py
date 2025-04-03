import base64
import binascii

base64_hash = "Q07RvZL+f+U8A/j74tbXVrQmQr0ZT5x6motXfodZT7Q="
bytes_data = base64.b64decode(base64_hash)
hex_hash = binascii.hexlify(bytes_data).decode('utf-8')

print(hex_hash)