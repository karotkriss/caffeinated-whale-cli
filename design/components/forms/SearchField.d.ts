/** The fleet / cache search box. */
export interface SearchFieldProps extends React.InputHTMLAttributes<HTMLInputElement> {
  placeholder?: string;
  /** Key hint shown while unfocused. Pass "" to hide. */
  shortcut?: string;
  style?: React.CSSProperties;
}
export declare function SearchField(props: SearchFieldProps): JSX.Element;
