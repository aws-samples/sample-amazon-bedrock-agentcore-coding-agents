import { type HTMLAttributes, type ReactNode, forwardRef, useRef, useEffect } from 'react';
import { cn } from '@foxl/ui';
import { Button } from '@foxl/ui';
import { ArrowUp, Square, Paperclip, Mic, Globe, Sparkles } from 'lucide-react';

export interface PromptInputProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

export const PromptInput = forwardRef<HTMLDivElement, PromptInputProps>(
  ({ className, children, ...props }, ref) => (
    <div
      ref={ref}
      className={cn('bg-background pt-3 pb-4', className)}
      {...props}
    >
      <div className="max-w-3xl mx-auto px-4">
        {children}
      </div>
    </div>
  )
);
PromptInput.displayName = 'PromptInput';

export interface PromptInputFormProps extends HTMLAttributes<HTMLFormElement> {
  onSubmit: () => void;
  children: ReactNode;
}

export const PromptInputForm = forwardRef<HTMLFormElement, PromptInputFormProps>(
  ({ className, onSubmit, children, ...props }, ref) => (
    <form
      ref={ref}
      className={cn(
        // White composer surface (bg-card = white in light mode) instead of the
        // grey bg-muted/30, so the message bar reads as a clean input, not a panel.
        'relative rounded-2xl border bg-card shadow-sm',
        'focus-within:ring-1 focus-within:ring-ring/20 transition-all duration-200',
        className
      )}
      onSubmit={(e) => {
        e.preventDefault();
      }}
      {...props}
    >
      {children}
    </form>
  )
);
PromptInputForm.displayName = 'PromptInputForm';

export interface PromptInputTextareaProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onPaste?: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
  placeholder?: string;
  disabled?: boolean;
  hasAttachments?: boolean;
  className?: string;
  autoFocus?: boolean;
  /** Change this value to re-trigger focus (e.g. pass conversationId). */
  focusKey?: string | number | null;
}

let _textareaEl: HTMLTextAreaElement | null = null;
export function getPromptTextareaValue(): string {
  return _textareaEl?.value || '';
}

const IS_MOBILE = typeof navigator !== 'undefined' && /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

export const PromptInputTextarea = ({
  value,
  onChange,
  onSubmit,
  onPaste,
  placeholder = 'Ask anything...',
  disabled,
  hasAttachments,
  className,
  autoFocus,
  focusKey,
}: PromptInputTextareaProps) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const onChangeRef = useRef(onChange);
  const onSubmitRef = useRef(onSubmit);
  const onPasteRef = useRef(onPaste);
  const hasAttachmentsRef = useRef(hasAttachments);
  const disabledRef = useRef(disabled);
  onChangeRef.current = onChange;
  onSubmitRef.current = onSubmit;
  onPasteRef.current = onPaste;
  hasAttachmentsRef.current = hasAttachments;
  disabledRef.current = disabled;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const ta = document.createElement('textarea');
    ta.rows = 1;
    ta.placeholder = placeholder;
    ta.value = value;
    if (disabled) ta.disabled = true;
    ta.className = [
      'w-full resize-none bg-transparent px-4 py-3.5',
      'text-sm leading-relaxed placeholder:text-muted-foreground/50',
      'focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed',
      className || '',
    ].join(' ');
    ta.style.minHeight = '52px';
    ta.style.maxHeight = '200px';
    ta.style.background = 'transparent';
    ta.style.border = 'none';
    ta.style.outline = 'none';
    ta.style.overflow = 'hidden';

    const resize = () => {
      ta.style.height = 'auto';
      ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
    };

    // Both platforms must push the value to the parent on every input so the
    // send button (disabled on !draft.trim()) enables as you type and handleSend
    // sees the real text. Mobile previously only fired onChange on `blur`, so
    // the parent's draft stayed empty while typing - the send button never
    // enabled and a tap sent nothing. The difference between platforms is only
    // IME composition + Enter-to-send (desktop), NOT when the value propagates.
    {
      let composing = false;
      ta.addEventListener('compositionstart', () => { composing = true; });
      ta.addEventListener('compositionend', () => {
        composing = false;
        onChangeRef.current(ta.value);
        resize();
      });
      ta.addEventListener('input', () => {
        resize();
        if (!composing) onChangeRef.current(ta.value);
      });
      // Safety net: ensure the final value is committed when focus leaves
      // (e.g. an IME that does not fire compositionend before blur on mobile).
      ta.addEventListener('blur', () => {
        if (ta.value) onChangeRef.current(ta.value);
      });
      if (!IS_MOBILE) {
        // Desktop only: Enter submits, Shift+Enter newlines. On mobile Enter
        // inserts a newline and the user taps the send button.
        ta.addEventListener('keydown', (e) => {
          if (composing || e.isComposing) return;
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if ((ta.value.trim() || hasAttachmentsRef.current) && !disabledRef.current) {
              onSubmitRef.current();
              setTimeout(() => ta.focus({ preventScroll: true }), 0);
            }
          }
        });
      }
    }

    ta.addEventListener('paste', (e) => {
      onPasteRef.current?.(e as any);
    });

    container.appendChild(ta);
    textareaRef.current = ta;
    _textareaEl = ta;
    if (autoFocus) ta.focus({ preventScroll: true });

    return () => {
      container.removeChild(ta);
      textareaRef.current = null;
      _textareaEl = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Push EXTERNAL value changes into the imperatively-created textarea. The
  // textarea is uncontrolled (created in the mount effect), so a programmatic
  // setDraft (clicking a suggestion chip, restoring a draft) never reached
  // the DOM before: only the value==='' clear path was handled, so chips did
  // nothing. Now any value that differs from the textarea's current content is
  // written in and the box is re-sized. When the user types, value already
  // equals ta.value (they typed it), so this is a no-op and never fights input.
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta || ta.value === value) return;
    ta.value = value;
    ta.style.height = 'auto';
    if (value) ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
  }, [value]);

  useEffect(() => {
    if (autoFocus && !disabled && textareaRef.current) {
      textareaRef.current.focus({ preventScroll: true });
    }
  }, [autoFocus, disabled]);

  useEffect(() => {
    if (focusKey !== undefined && textareaRef.current && !disabled) {
      requestAnimationFrame(() => textareaRef.current?.focus({ preventScroll: true }));
    }
  }, [focusKey, disabled]);

  useEffect(() => {
    if (textareaRef.current) textareaRef.current.disabled = !!disabled;
  }, [disabled]);

  return <div ref={containerRef} />;
};

export interface PromptInputActionsProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

export const PromptInputActions = ({ className, children, ...props }: PromptInputActionsProps) => (
  <div
    className={cn('flex items-center justify-between px-3 pb-2.5', className)}
    {...props}
  >
    {children}
  </div>
);

export interface PromptInputLeftActionsProps extends HTMLAttributes<HTMLDivElement> {
  children?: ReactNode;
}

export const PromptInputLeftActions = ({ className, children, ...props }: PromptInputLeftActionsProps) => (
  <div
    className={cn('flex items-center gap-1', className)}
    {...props}
  >
    {children}
  </div>
);

export interface PromptInputRightActionsProps extends HTMLAttributes<HTMLDivElement> {
  children?: ReactNode;
}

export const PromptInputRightActions = ({ className, children, ...props }: PromptInputRightActionsProps) => (
  <div
    className={cn('flex items-center gap-1', className)}
    {...props}
  >
    {children}
  </div>
);

export interface PromptInputSubmitProps {
  disabled?: boolean;
  isStreaming?: boolean;
  onStop?: () => void;
  onSubmit?: () => void;
}

export const PromptInputSubmit = ({ disabled, isStreaming, onStop, onSubmit }: PromptInputSubmitProps) => {
  if (isStreaming) {
    return (
      <Button
        type="button"
        size="icon"
        variant="default"
        className="h-8 w-8 rounded-lg"
        onClick={onStop}
      >
        <Square className="h-3.5 w-3.5" fill="currentColor" />
      </Button>
    );
  }

  return (
    <Button
      type="button"
      size="icon"
      disabled={disabled}
      onClick={onSubmit}
      className={cn(
        'h-8 w-8 rounded-lg transition-all duration-200',
        disabled
          ? 'bg-zinc-900 dark:bg-zinc-100 text-zinc-100 dark:text-zinc-900 opacity-30 cursor-not-allowed'
          : 'bg-zinc-900 dark:bg-zinc-100 text-zinc-100 dark:text-zinc-900 hover:opacity-80'
      )}
    >
      <ArrowUp className="h-4 w-4" />
    </Button>
  );
};

export interface PromptInputToolButtonProps {
  icon: ReactNode;
  label: string;
  active?: boolean;
  onClick?: () => void;
  disabled?: boolean;
}

export const PromptInputToolButton = ({ icon, label, active, onClick, disabled }: PromptInputToolButtonProps) => (
  <Button
    type="button"
    size="icon"
    variant="ghost"
    disabled={disabled}
    className={cn(
      'h-8 w-8 rounded-lg transition-colors',
      active
        ? 'bg-primary/10 text-primary'
        : 'text-muted-foreground hover:text-foreground hover:bg-muted'
    )}
    onClick={onClick}
  >
    {icon}
    <span className="sr-only">{label}</span>
  </Button>
);

export const PromptInputAttachButton = ({ disabled, htmlFor }: { disabled?: boolean; htmlFor: string }) => (
  <label
    htmlFor={disabled ? undefined : htmlFor}
    className={cn(
      'inline-flex items-center justify-center h-8 w-8 rounded-lg transition-colors',
      disabled
        ? 'opacity-50 cursor-not-allowed pointer-events-none'
        : 'cursor-pointer text-muted-foreground hover:text-foreground hover:bg-muted'
    )}
    role="button"
    aria-label="Attach file"
    tabIndex={disabled ? -1 : 0}
    onKeyDown={(e) => {
      if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        document.getElementById(htmlFor)?.click();
      }
    }}
  >
    <Paperclip className="h-4 w-4" />
  </label>
);

export const PromptInputMicButton = ({ active, onClick, disabled }: { active?: boolean; onClick?: () => void; disabled?: boolean }) => (
  <PromptInputToolButton
    icon={<Mic className="h-4 w-4" />}
    label="Voice input"
    active={active}
    onClick={onClick}
    disabled={disabled}
  />
);

export const PromptInputWebButton = ({ active, onClick, disabled }: { active?: boolean; onClick?: () => void; disabled?: boolean }) => (
  <PromptInputToolButton
    icon={<Globe className="h-4 w-4" />}
    label="Web search"
    active={active}
    onClick={onClick}
    disabled={disabled}
  />
);

export interface PromptInputFooterProps extends HTMLAttributes<HTMLParagraphElement> {
  children: ReactNode;
}

export const PromptInputFooter = ({ className, children, ...props }: PromptInputFooterProps) => (
  <p className={cn('text-[11px] text-muted-foreground/60 text-center mt-2 flex items-center justify-center gap-1', className)} {...props}>
    <Sparkles className="h-3 w-3" />
    {children}
  </p>
);
