import { useState, type FormEvent } from "react";

export function MessageInput({
  disabled,
  onSubmit,
  onInterrupt,
}: {
  disabled: boolean;
  onSubmit: (text: string) => void;
  onInterrupt: () => void;
}) {
  const [value, setValue] = useState("");

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const text = value.trim();
    if (!text) return;
    setValue("");
    onSubmit(text);
  };

  return (
    <form className={`input-row ${value.startsWith("!") ? "shell" : ""}`} onSubmit={submit}>
      <input
        className="msg-input"
        placeholder={disabled ? "No active PAI" : "Message PAI or type !command"}
        value={value}
        disabled={disabled}
        autoFocus
        onChange={(e) => setValue(e.target.value)}
      />
      <button className="input-button secondary" type="button" disabled={disabled} onClick={onInterrupt}>
        Interrupt
      </button>
      <button className="input-button primary" type="submit" disabled={disabled || !value.trim()}>
        Send
      </button>
    </form>
  );
}
