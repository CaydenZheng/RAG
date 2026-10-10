import { Fragment } from "react";

const HTTP_LINK = /(https?:\/\/[^\s<>"']+)/g;

export function SafeText({ value }: { value: string }) {
  const parts = value.split(HTTP_LINK);
  return parts.map((part, index) => {
    if (!part.startsWith("http://") && !part.startsWith("https://")) {
      return <Fragment key={`${index}-${part.length}`}>{part}</Fragment>;
    }
    return (
      <a key={`${index}-${part}`} href={part} target="_blank" rel="noopener noreferrer">
        {part}
      </a>
    );
  });
}
