const form = document.getElementById("chat-form");
const input = document.getElementById("message");
const chat = document.getElementById("chat");
const send = document.getElementById("send");

function addMessage(text, role) {
  const message = document.createElement("div");

  message.className = `message ${role}`;
  message.textContent = text;

  chat.appendChild(message);

  chat.scrollTop = chat.scrollHeight;

  return message;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = input.value.trim();

  if (!message) {
    return;
  }

  addMessage(message, "user");

  input.value = "";
  send.disabled = true;

  const thinking = addMessage("Thinking...", "assistant");

  try {
    const response = await fetch("/chat", {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        message: message,
      }),
    });

    const data = await response.json();

    thinking.textContent = data.response;
  } catch (error) {
    thinking.textContent = "I couldn't reach the SAGE backend.";
  }

  send.disabled = false;
  input.focus();
});
