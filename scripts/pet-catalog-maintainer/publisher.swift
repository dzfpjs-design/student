import Foundation
import Security
import CryptoKit
import LocalAuthentication

// Offline project-maintainer signer. The app receives only the public key.
let service = "org.home-study-companion.maintainer.catalog-signing"
let account = "ed25519-project-maintainer-v1"
let keyID = "hsc-maintainer-20261024-v1"
let issuer = "家庭学习伙伴项目维护者"
struct PublisherFailure: Error { let code: String }
func fail(_ code: String) throws -> Never { throw PublisherFailure(code: code) }
func key(_ mayCreate: Bool) throws -> (Curve25519.Signing.PrivateKey, Bool) {
    let authentication = LAContext()
    authentication.interactionNotAllowed = true
    let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service, kSecAttrAccount as String: account,
        kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne,
        kSecUseAuthenticationContext as String: authentication]
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecSuccess {
        guard let data = result as? Data, data.count == 32 else { try fail("KEYCHAIN_ITEM_INVALID") }
        return (try Curve25519.Signing.PrivateKey(rawRepresentation: data), false)
    }
    guard status == errSecItemNotFound else { try fail("KEYCHAIN_READ_STATUS_\(status)") }
    guard mayCreate else { try fail("KEYCHAIN_BOOTSTRAP_REQUIRED") }
    let created = Curve25519.Signing.PrivateKey()
    let item: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service, kSecAttrAccount as String: account,
        kSecAttrLabel as String: "家庭学习伙伴项目目录签名（维护者）",
        kSecValueData as String: created.rawRepresentation,
        kSecUseAuthenticationContext as String: authentication]
    let added = SecItemAdd(item as CFDictionary, nil)
    guard added == errSecSuccess else { try fail("KEYCHAIN_CREATE_STATUS_\(added)") }
    // Read back the same dedicated item. Never overwrite an existing item.
    let reread = try key(false)
    guard reread.0.publicKey.rawRepresentation == created.publicKey.rawRepresentation else { try fail("KEYCHAIN_READBACK_MISMATCH") }
    return (reread.0, true)
}
func canonical(_ object: Any) throws -> Data {
    try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
}
func publicPEM(_ publicKey: Curve25519.Signing.PublicKey) -> String {
    let prefix: [UInt8] = [0x30,0x2a,0x30,0x05,0x06,0x03,0x2b,0x65,0x70,0x03,0x21,0x00]
    let encoded = (Data(prefix) + publicKey.rawRepresentation).base64EncodedString()
    return "-----BEGIN PUBLIC KEY-----\n" + encoded + "\n-----END PUBLIC KEY-----\n"
}
func payloadFromStdin() throws -> Data {
    var bytes = Data()
    while true {
        let part = FileHandle.standardInput.readData(ofLength: 4096)
        if part.isEmpty { break }
        bytes.append(part)
        guard bytes.count <= 12000 else { try fail("PAYLOAD_BOUND") }
    }
    guard !bytes.isEmpty, bytes.allSatisfy({ $0 >= 32 && $0 <= 126 }),
          let value = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
          try canonical(value) == bytes,
          value["schema"] as? String == "pet_audio_catalog_v1",
          value["region"] as? String == "cn-beijing", value["currency"] as? String == "CNY",
          value["endpoint_family"] as? String == "aliyun-compatible-cn-beijing-v1",
          value["protocol"] as? String == "qwen_omni_audio_json_sse_v1" else { try fail("REVIEWED_CANONICAL_PAYLOAD_REQUIRED") }
    return bytes
}
do {
    guard CommandLine.arguments.count == 2 else { try fail("ONE_COMMAND_REQUIRED") }
    switch CommandLine.arguments[1] {
    case "source-only":
        FileHandle.standardOutput.write(try canonical(["schema":1, "keychain_accessed":false, "network_used":false]))
    case "bootstrap":
        let (privateKey, created) = try key(true)
        FileHandle.standardOutput.write(try canonical(["schema":1,"key_id":keyID,"issuer":issuer,
            "public_key_pem":publicPEM(privateKey.publicKey),"keychain_item_created":created,
            "private_key_exported":false,"network_used":false]))
    case "sign-reviewed-stdin":
        let payload = try payloadFromStdin()
        let (privateKey, _) = try key(false)
        let signature = try privateKey.signature(for: payload)
        guard privateKey.publicKey.isValidSignature(signature, for: payload) else { try fail("SIGNATURE_READBACK_MISMATCH") }
        let envelope: [String: Any] = ["schema":"pet_audio_catalog_signed_v1", "key_id":keyID,
            "payload_base64":payload.base64EncodedString(), "signature_base64":signature.base64EncodedString()]
        let encoded = try canonical(envelope)
        guard encoded.count <= 16384 else { try fail("ENVELOPE_BOUND") }
        FileHandle.standardOutput.write(encoded)
    default: try fail("COMMAND_NOT_SUPPORTED")
    }
} catch {
    let code = (error as? PublisherFailure)?.code ?? "PUBLISHER_OPERATION_FAILED"
    FileHandle.standardError.write(Data((code + "\n").utf8))
    exit(1)
}
