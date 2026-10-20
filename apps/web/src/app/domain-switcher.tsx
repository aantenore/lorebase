import { useNavigate } from '@tanstack/react-router';
import { useDomains } from '@/api/queries';
import { Select } from '@/ui/field';

/** Switching Domain drops the Branch: Branches belong to one Domain. */
export function DomainSwitcher({ domain }: { domain: string }) {
  const navigate = useNavigate();
  const { data } = useDomains();
  const options = (data?.items ?? [{ id: domain, display_name: domain }]).map((d) => ({
    value: d.id,
    label: d.display_name,
  }));

  return (
    <Select
      label="Domain"
      hideLabel
      value={domain}
      options={options}
      onChange={(event) => {
        void navigate({ to: '/$domain/explorer', params: { domain: event.target.value } });
      }}
    />
  );
}
