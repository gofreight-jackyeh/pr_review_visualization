import MarkdownIt from "markdown-it"

/**
 * Markdown from an untrusted source (a PR description). Safe by construction, so the output can be set as HTML:
 * html:false escapes every raw HTML tag as text (no <script>, no onerror attributes); links are http(s)/mailto only
 * (anything else stays plain text) and open in a new tab; images are not loaded (they render as a link).
 */
const md = new MarkdownIt({ html: false, linkify: true, breaks: true }).disable("image")
md.validateLink = (url) => /^(https?:|mailto:)/i.test(url.trim())
const linkOpen = md.renderer.rules.link_open ?? ((tokens, idx, opts, _env, self) => self.renderToken(tokens, idx, opts))
md.renderer.rules.link_open = (tokens, idx, opts, env, self) => {
  tokens[idx].attrSet("target", "_blank")
  tokens[idx].attrSet("rel", "noreferrer noopener")
  return linkOpen(tokens, idx, opts, env, self)
}

export const renderMarkdown = (text: string) => md.render(text)

export function Markdown({ text, className }: { text: string; className?: string }) {
  return <div className={"markdown " + (className ?? "")} data-testid="markdown" dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />
}
