from aiops.util import format_memory_mib, image_repository, parse_memory_to_bytes, tokenize


def test_parse_binary_suffixes():
    assert parse_memory_to_bytes("64Mi") == 64 * 1024 ** 2
    assert parse_memory_to_bytes("1Gi") == 1024 ** 3
    assert parse_memory_to_bytes("512M") == 512 * 1000 ** 2
    assert parse_memory_to_bytes("134217728") == 134217728


def test_format_memory():
    assert format_memory_mib(64 * 1024 ** 2) == "64Mi"
    assert format_memory_mib(1024 ** 3) == "1Gi"
    assert format_memory_mib(512 * 1024 ** 2) == "512Mi"


def test_oom_scale():
    doubled = parse_memory_to_bytes("64Mi") * 2
    assert format_memory_mib(doubled) == "128Mi"


def test_tokenize():
    assert tokenize("OOMKilled memory") == {"oomkilled", "memory"}


def test_image_repository():
    assert image_repository("nginx:notfound") == "nginx"
    assert image_repository("nginx") == "nginx"
    assert image_repository("docker.io/library/nginx:1.27") == "docker.io/library/nginx"
    assert image_repository("localhost:5000/app:v1") == "localhost:5000/app"
    assert image_repository("localhost:5000/app") == "localhost:5000/app"
    assert image_repository("app@sha256:abc") == "app"
