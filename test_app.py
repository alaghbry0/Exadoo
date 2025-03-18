from web3 import Web3

# الاتصال بشبكة BSC Testnet
w3 = Web3(Web3.HTTPProvider("https://bsc-testnet.publicnode.com"))
assert w3.is_connected(), "فشل الاتصال بالشبكة!"

# تحويل العناوين إلى صيغة Checksum
mock_token_address = w3.to_checksum_address("0x567a9bcbe6706be5c24513784bfe46631e8f7aa3")  # عنوان العقد المنشور
receiver_address = w3.to_checksum_address("0x9AFfD3b00e1561aD84dA519920ECaB0Ac94E9e39")

# تعريف ABI لوظيفة transfer كما هو شائع في عقود ERC20
abi = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
        "payable": False,
        "stateMutability": "nonpayable"
    }
]

# الاتصال بالعقد
contract = w3.eth.contract(address=mock_token_address, abi=abi)

# الحساب المرسل (استخدم مفتاحك الخاص الصحيح)
sender_private_key = "0x7825271b80c34908fdc628d4248a9d9714fe0c9ced561461779ee9445cbd8488"  # استبدل هذا بمفتاحك الخاص
sender_account = w3.eth.account.from_key(sender_private_key)

# بناء المعاملة لتحويل 100 توكن (مع 18 منزلة عشرية)
tx = contract.functions.transfer(
    receiver_address,
    25 * 10**18  # 100 توكن
).build_transaction({
    'chainId': 97,
    'gas': 100000,
    'gasPrice': w3.to_wei('5', 'gwei'),
    'nonce': w3.eth.get_transaction_count(sender_account.address),
})

# توقيع وإرسال المعاملة
signed_tx = sender_account.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
print(f"✅ Success! TX Hash: {tx_hash.hex()}")
print(f"🔗 Track: https://testnet.bscscan.com/tx/0x{tx_hash.hex()}")

# الانتظار للحصول على إيصال المعاملة والتحقق من حالتها
receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
print("Transaction Receipt:", receipt)
