"""Tests for small deterministic route declaration recognizers."""

from sydes.core.models import CandidateFileRead, ReadFileSnippet
from sydes.discover.deterministic_routes import extract_deterministic_routes


def _candidate(repo: str, path: str, text: str, role: str = "source_route_candidate") -> CandidateFileRead:
    return CandidateFileRead(
        repo=repo,
        relative_path=path,
        role=role,
        snippet=ReadFileSnippet(
            repo=repo,
            relative_path=path,
            text=text,
            line_count=len(text.splitlines()),
            char_count=len(text),
        ),
    )


def test_extract_flask_routes_and_ignore_test_invocations() -> None:
    source = _candidate(
        "api",
        "app/routes.py",
        "\n".join(
            [
                "@app.route('/')",
                "def hello():",
                "    return 'hello'",
                "@bp.route('/items', methods=['GET'])",
                "def get_items():",
                "    return []",
                "@bp.route('/items/<int:item_id>', methods=['GET'])",
                "def get_item(item_id):",
                "    return item_id",
                "@bp.route('/items', methods=['POST'])",
                "def add_item():",
                "    return {}",
            ]
        ),
    )
    test_file = _candidate(
        "api",
        "tests/test_app.py",
        "response = client.get('/items/0')\nresponse = client.post('/items', json={})\n",
        role="test_usage_candidate",
    )
    endpoints, frameworks = extract_deterministic_routes([source, test_file])
    keys = {(item.method, item.path, item.file) for item in endpoints}
    assert ("GET", "/", "app/routes.py") in keys
    assert ("GET", "/items", "app/routes.py") in keys
    assert ("GET", "/items/{item_id}", "app/routes.py") in keys
    assert ("POST", "/items", "app/routes.py") in keys
    assert all(item.file != "tests/test_app.py" for item in endpoints)
    assert "flask_fastapi" in frameworks


def test_extract_fastapi_routes() -> None:
    source = _candidate(
        "api",
        "main.py",
        "\n".join(
            [
                "@app.get('/users/')",
                "def get_users():",
                "    return []",
                "@router.post('/users/')",
                "def create_user():",
                "    return {}",
                "@router.get('/items/{item_id}')",
                "def get_item(item_id):",
                "    return {}",
            ]
        ),
    )
    endpoints, _ = extract_deterministic_routes([source])
    keys = {(item.method, item.path) for item in endpoints}
    assert ("GET", "/users") in keys
    assert ("POST", "/users") in keys
    assert ("GET", "/items/{item_id}") in keys


def test_extract_express_routes_and_ignore_invocations() -> None:
    source = _candidate(
        "api",
        "src/routes/users.ts",
        "\n".join(
            [
                'app.get("/items", getItems)',
                'router.get("/items/:id", handler)',
                'router.post("/items", async (req, res) => {})',
                'request(app).get("/items")',
                'supertest(app).post("/items")',
            ]
        ),
    )
    endpoints, frameworks = extract_deterministic_routes([source])
    keys = {(item.method, item.path) for item in endpoints}
    assert ("GET", "/items") in keys
    assert ("GET", "/items/{id}") in keys
    assert ("POST", "/items") in keys
    assert "express" in frameworks


def test_extract_spring_routes_with_class_prefix() -> None:
    source = _candidate(
        "service1",
        "src/main/java/com/example/BookController.java",
        "\n".join(
            [
                '@RequestMapping("/db")',
                "public class BookController {",
                '  @GetMapping("/books")',
                "  public List<Book> getBooks() { return List.of(); }",
                '  @PostMapping("/users")',
                "  public User createUser() { return new User(); }",
                "}",
            ]
        ),
    )
    endpoints, frameworks = extract_deterministic_routes([source])
    keys = {(item.method, item.path, item.handler) for item in endpoints}
    assert ("GET", "/db/books", "getBooks") in keys
    assert ("POST", "/db/users", "createUser") in keys
    assert "spring" in frameworks
