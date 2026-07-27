const SESSION_KEY = "leo_workspace_session";
let mode = "login";

const form = document.querySelector("#auth-form");
const loginTab = document.querySelector("#login-tab");
const registerTab = document.querySelector("#register-tab");
const confirmRow = document.querySelector("#confirm-row");
const workspaceRow = document.querySelector("#workspace-row");
const confirmPassword = document.querySelector("#confirm-password");
const workspaceName = document.querySelector("#workspace-name");
const submit = document.querySelector("#submit");
const message = document.querySelector("#message");

const setMode = (next) => {
  mode = next;
  const registering = mode === "register";
  loginTab.classList.toggle("active", !registering);
  registerTab.classList.toggle("active", registering);
  confirmRow.classList.toggle("hidden", !registering);
  workspaceRow.classList.toggle("hidden", !registering);
  confirmPassword.required = registering;
  workspaceName.required = registering;
  document.querySelector("#title").textContent = registering ? "Регистрация" : "Вход";
  document.querySelector("#subtitle").textContent = registering
    ? "Создайте отдельное рабочее пространство для своего магазина."
    : "Введите логин и пароль своего рабочего пространства.";
  submit.textContent = registering ? "Создать аккаунт" : "Войти";
  message.textContent = "";
};

const readError = async (response) => {
  try {
    const payload = await response.json();
    return typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`;
  } catch (_) {
    return `HTTP ${response.status}`;
  }
};

loginTab.addEventListener("click", () => setMode("login"));
registerTab.addEventListener("click", () => setMode("register"));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  message.textContent = "";
  const username = document.querySelector("#username").value.trim();
  const password = document.querySelector("#password").value;
  if (mode === "register" && password !== confirmPassword.value) {
    message.textContent = "Пароли не совпадают.";
    return;
  }

  submit.disabled = true;
  submit.textContent = mode === "register" ? "Создаю…" : "Вхожу…";
  const body = mode === "register"
    ? {username, password, workspace_name: workspaceName.value.trim()}
    : {username, password};
  try {
    const response = await fetch(`/api/auth/${mode}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(await readError(response));
    const payload = await response.json();
    localStorage.setItem(SESSION_KEY, payload.access_token);
    window.location.assign("/crm/account");
  } catch (error) {
    message.textContent = error.message || "Не удалось выполнить вход.";
  } finally {
    submit.disabled = false;
    submit.textContent = mode === "register" ? "Создать аккаунт" : "Войти";
  }
});

if (localStorage.getItem(SESSION_KEY)) window.location.replace("/crm/account");
