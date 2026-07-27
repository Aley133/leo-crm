const workspaceToken = localStorage.getItem("leo_workspace_session");
const legacyToken = localStorage.getItem("leo_crm_service_token");
const message = document.querySelector("#message");

const route = async () => {
  if (workspaceToken) {
    try {
      const response = await fetch("/api/auth/me", {
        headers: {Authorization: `Bearer ${workspaceToken}`},
        cache: "no-store",
      });
      if (response.ok) {
        window.location.replace("/crm/workspace/orders");
        return;
      }
      localStorage.removeItem("leo_workspace_session");
    } catch (_) {
      message.textContent = "Не удалось проверить пользовательскую сессию.";
      return;
    }
  }
  if (legacyToken) {
    window.location.replace("/crm/legacy");
    return;
  }
  window.location.replace("/login");
};

route();
