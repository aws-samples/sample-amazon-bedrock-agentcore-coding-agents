import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "../lib/utils";

const badgeVariants = cva(
  // Vercel metadata pill: rounded-full, mono-leaning caption, no drop shadow.
  // Tracking is slightly tight so short labels read as a typeset chip.
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium tracking-[-0.01em] transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground hover:bg-primary/90",
        secondary:
          "border-border bg-secondary text-secondary-foreground hover:bg-secondary/80",
        destructive:
          "border-transparent bg-destructive/10 text-destructive hover:bg-destructive/15",
        // Soft status pills: tinted fill + matching text, the brand way to show
        // "live" / "passed" / "pending" without a sixth accent colour.
        success:
          "border-transparent bg-success/10 text-success hover:bg-success/15",
        warning:
          "border-transparent bg-warning/15 text-warning hover:bg-warning/20",
        outline: "border-border text-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
