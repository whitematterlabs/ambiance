import { useState } from "react";

export function MessageInput({
  disabled,
  onSubmit,
}: {
  disabled: boolean;
  onSubmit: (text: string) => void;
}) {
  const [value, setValue] = useState("");

  const submit = () => {
    const text = value.trim();
    if (!text) return;
    setValue("");
    onSubmit(text);
  };

  return (
    <div className={`input-row ${value.startsWith("!") ? "shell" : ""}`}>
      <input
        className="msg-input"
        placeholder={
          disabled
            ? "no PAI tab active"
            : "message PAI…  (Enter to send · !cmd to run shell · Esc to interrupt)"
        }
        value={value}
        disabled={disabled}
        autoFocus
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        }}
      />
    </div>
  );
}
