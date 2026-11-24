from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_new_workspace_has_only_one_supervisor_and_direct_chat(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "workspace.sqlite3"))
    with TestClient(app) as client:
        response = client.get("/workspace")
        assert response.status_code == 200
        workspace = response.json()
        assert [agent["kind"] for agent in workspace["agents"]] == ["supervisor"]
        assert workspace["agents"][0]["id"] == workspace["supervisor_id"]
        assert len(workspace["conversations"]) == 1
        direct = workspace["conversations"][0]
        assert direct["kind"] == "direct"
        assert direct["id"] == workspace["direct_conversation_id"]
        assert direct["member_ids"] == [workspace["supervisor_id"]]
        assert workspace["active_runs"] == []
