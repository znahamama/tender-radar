import pytest


@pytest.mark.django_db
def test_admin_login_page_loads(client):
    assert client.get("/admin/login/").status_code == 200
