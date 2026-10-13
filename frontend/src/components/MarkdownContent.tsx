import type { ComponentPropsWithoutRef, ReactNode } from "react";
import ReactMarkdown, { type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import { safeHttpUrl } from "../api/safeUrls";

function safeLink(value: string): string {
  return safeHttpUrl(value) ?? "";
}

function MarkdownLink({
  href,
  children,
  node: _node,
  ...props
}: ComponentPropsWithoutRef<"a"> & ExtraProps & { children?: ReactNode }) {
  void _node;
  if (!href) return <>{children}</>;
  return (
    <a {...props} href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

export function MarkdownContent({ value }: { value: string }) {
  return (
    <div className="message-body answer-body markdown-content">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        urlTransform={safeLink}
        components={{ a: MarkdownLink }}
      >
        {value}
      </ReactMarkdown>
    </div>
  );
}
