type AvatarProps = {
  src?: string;
  name: string;
  size?: "sm" | "md" | "lg";
};

export function Avatar({ src, name, size = "md" }: AvatarProps) {
  if (src) {
    return <img className={`avatar avatar-${size}`} src={src} alt={name} />;
  }
  return (
    <span className={`avatar avatar-${size} avatar-initials`}>
      {name.slice(0, 2).toUpperCase()}
    </span>
  );
}
