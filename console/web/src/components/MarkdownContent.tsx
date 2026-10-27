import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

export function MarkdownContent({ text, streaming = false }: { text: string; streaming?: boolean }) {
  return <div className={`prose-chat${streaming ? ' md-stream' : ''}`}>
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} skipHtml>
      {text}
    </ReactMarkdown>
  </div>;
}
