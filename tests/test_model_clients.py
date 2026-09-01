"""验证模型客户端的兼容接口地址规范化。"""

from jd_knowledge.pipeline.models import _endpoint


def test_openai_compatible_endpoint_normalization() -> None:
    assert _endpoint("http://service/v1", "embeddings") == "http://service/v1/embeddings"
    assert _endpoint("http://service/v1/embeddings", "embeddings") == "http://service/v1/embeddings"
    assert _endpoint("http://service", "rerank") == "http://service/v1/rerank"
