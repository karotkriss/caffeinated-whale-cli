/** One line of the fleet delta log. */
export interface EventRowProps {
  /** INSTANT = Docker's event stream, FAST = the health poll, ACTION = a click, ERROR = a failed read. */
  tier?: "INSTANT" | "FAST" | "ACTION" | "ERROR";
  project: string;
  message: string;
  /** Pre-formatted clock time; the component does no formatting. */
  time?: string;
  /** Plays the one-shot arrival tint. */
  fresh?: boolean;
  style?: React.CSSProperties;
}
export declare function EventRow(props: EventRowProps): JSX.Element;
