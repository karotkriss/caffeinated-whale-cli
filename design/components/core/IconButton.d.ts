/** A square, label-less control. Always give it a `title` — it is the accessible name. */
export interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Lucide icon name. */
  icon: string;
  size?: "sm" | "md" | "lg";
  variant?: "ghost" | "secondary" | "danger";
  /** Tooltip text and accessible label. Required in practice. */
  title?: string;
  disabled?: boolean;
}
export declare function IconButton(props: IconButtonProps): JSX.Element;
