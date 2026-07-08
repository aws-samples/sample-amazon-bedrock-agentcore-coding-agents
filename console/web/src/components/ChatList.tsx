import { useNavigate, useParams } from 'react-router-dom';
import { X } from 'lucide-react';
import { useChats, removeChat } from '../hooks/useChats';

/**
 * The sub-chat list under the "Chat" sidebar item: one row per conversation
 * (uuid), newest first, like Codex's "Chats". Clicking a row opens that
 * conversation; the x removes it. A fresh "New chat" is started from the Chat
 * page header, not here.
 */
export function ChatList() {
  const nav = useNavigate();
  const { chatId } = useParams<{ chatId?: string }>();
  const { chats } = useChats();

  if (chats.length === 0) return null;

  return (
    <div className="mt-0.5 flex flex-col gap-0.5 pl-7 pr-1">
      {chats.map((c) => (
        <div
          key={c.id}
          className={`group flex items-center gap-1 rounded-md pr-1 text-xs ${
            chatId === c.id ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/60'
          }`}
        >
          <button
            onClick={() => nav(`/fleets/c/${c.id}`)}
            className="min-w-0 flex-1 truncate py-1 pl-2 text-left"
            title={c.title}
          >
            {c.title}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              removeChat(c.id);
              if (chatId === c.id) nav('/fleets');
            }}
            title="Delete chat"
            className="rounded p-0.5 text-muted-foreground opacity-0 hover:bg-muted hover:text-destructive group-hover:opacity-100"
          >
            <X className="size-3" />
          </button>
        </div>
      ))}
    </div>
  );
}
