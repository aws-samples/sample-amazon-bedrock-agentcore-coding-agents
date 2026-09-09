import { useEffect, useRef, useState } from 'react';
import { ArrowUp, Check, ChevronDown, FileText, GitBranch, Paperclip, Square, X } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@foxl/ui';
import type { ModelOption } from '../../api';

interface ChatComposerProps {
  draft: string;
  onDraft: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  connected: boolean;
  connectionError?: string;
  loadingConnection: boolean;
  models: ModelOption[];
  model: string;
  onModel: (id: string) => void;
  modelError?: string;
  repo?: string;
  onSettings: () => void;
  attachments: { name: string; text: string }[];
  onFiles: (files: File[]) => void;
  onRemoveFile: (index: number) => void;
  attachmentError?: string;
}

export function ChatComposer(props: ChatComposerProps) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const files = useRef<HTMLInputElement>(null);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const selected = props.models.find(model => model.id === props.model);
  useEffect(() => {
    if (!textarea.current) return;
    textarea.current.style.height = 'auto';
    textarea.current.style.height = `${Math.min(220, Math.max(72, textarea.current.scrollHeight))}px`;
  }, [props.draft]);
  const canSend = props.connected && !!selected && (!!props.draft.trim() || props.attachments.length > 0);
  return <div className="chat-composer-wrap">
    <div className="chat-composer" onPaste={event => {
      const pasted = Array.from(event.clipboardData.files);
      if (pasted.length) { event.preventDefault(); props.onFiles(pasted); }
    }}>
      {props.attachments.length > 0 && <div className="chat-attachments">
        {props.attachments.map((file, index) => <span key={`${file.name}-${index}`} className="chat-file">
          <FileText size={15} aria-hidden="true" /><span>{file.name}</span>
          <button type="button" aria-label={`Remove ${file.name}`} disabled={props.streaming} onClick={() => props.onRemoveFile(index)}><X size={14} aria-hidden="true" /></button>
        </span>)}
      </div>}
      <textarea ref={textarea} id="orchestrator-prompt" aria-label="Message your coding team"
        placeholder="Ask, plan, or build…" value={props.draft} rows={2} readOnly={props.streaming}
        onChange={event => props.onDraft(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (!props.streaming && canSend) props.onSend();
          }
        }} />
      <div className="chat-composer-toolbar">
        <div className="chat-composer-tools">
          <button type="button" className="chat-icon-button" aria-label="Attach files" title="Attach up to 5 files"
            disabled={props.streaming || props.attachments.length >= 5} onClick={() => files.current?.click()}><Paperclip size={19} aria-hidden="true" /></button>
          <input ref={files} type="file" multiple hidden aria-label="Choose attachments"
            onChange={event => { if (event.target.files) props.onFiles(Array.from(event.target.files)); event.target.value = ''; }} />
          <button type="button" className="chat-repository" title={props.repo || 'Choose a repository'} onClick={props.onSettings}>
            <GitBranch size={15} aria-hidden="true" /><span>{props.repo || 'Repository'}</span>
          </button>
        </div>
        <div className="chat-composer-tools">
          <Popover open={modelMenuOpen} onOpenChange={setModelMenuOpen}>
            <PopoverTrigger asChild><button type="button" className="chat-model" disabled={props.streaming || !props.models.length}
              aria-label={`Coordinator model: ${selected?.label || 'unavailable'}`}>
              <span>{selected?.label || (props.modelError ? 'Model unavailable' : 'Loading models…')}</span><ChevronDown size={14} aria-hidden="true" />
            </button></PopoverTrigger>
            <PopoverContent align="end" className="chat-model-menu">
              <p className="chat-menu-label">Coordinator model</p>
              {props.models.map(model => <button type="button" key={model.id} className="chat-model-option"
                aria-pressed={model.id === props.model} onClick={() => { props.onModel(model.id); setModelMenuOpen(false); }}>
                <div><strong>{model.label}</strong>{model.hint && <span>{model.hint}</span>}</div>
                {model.id === props.model && <Check size={16} aria-hidden="true" />}
              </button>)}
            </PopoverContent>
          </Popover>
          {props.streaming ? <button className="chat-send" type="button" onClick={props.onStop} aria-label="Stop response" title="Stop response"><Square size={16} fill="currentColor" aria-hidden="true" /></button>
            : <button className="chat-send" type="button" onClick={props.onSend} disabled={!canSend} aria-label="Send message" title={props.connected ? 'Send message' : 'Waiting for the workshop host'}><ArrowUp size={21} aria-hidden="true" /></button>}
        </div>
      </div>
    </div>
    {props.attachmentError && <p className="chat-inline-error" role="alert">{props.attachmentError}</p>}
    {props.modelError && <p className="chat-inline-error" role="alert">{props.modelError}</p>}
    <div className="chat-composer-caption">
      {props.connectionError ? <span role="alert">{props.connectionError}</span>
        : props.loadingConnection ? <span role="status">Connecting to the workshop host…</span>
        : !props.connected ? <span>The workshop host is unavailable. Check its connection and try again.</span>
        : <span>{props.streaming ? 'Stopping the response does not stop a build already dispatched.' : 'Review the pull request evidence before accepting a change.'}</span>}
    </div>
  </div>;
}
