import classnames from "classnames";

type LegacyBannerProps = {
  message: string;
  dismissed?: boolean;
};

export function LegacyBanner({ message, dismissed = false }: LegacyBannerProps) {
  return (
    <div className={classnames("legacy-banner", dismissed && "legacy-banner-dismissed")}>
      {message}
    </div>
  );
}
