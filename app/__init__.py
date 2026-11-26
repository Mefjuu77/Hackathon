import truststore

# Use the OS certificate store, so TLS works behind corporate proxies with their own root CA.
truststore.inject_into_ssl()
