import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactNode } from 'react';
import { Button } from './button';
import { AUTOFOCUS, Dialog } from './dialog';
import { Input, Select } from './field';
import { RouteFallback } from './route-fallback';
import { Table, TBody, TD, TH, THead, TR } from './table';
import { Tabs } from './tabs';
import { ToastProvider, useToast } from './toast';

function DialogHarness({ children = <Button>Inside</Button> }: { children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        onClick={() => {
          setOpen(true);
        }}
      >
        Open
      </Button>
      <Dialog
        open={open}
        title="Confirm"
        onClose={() => {
          setOpen(false);
        }}
      >
        {children}
      </Dialog>
    </>
  );
}

describe('ui primitives', () => {
  it('labels inputs and selects', () => {
    render(
      <>
        <Input label="Name" hint="Legal name" />
        <Select label="Kind" options={[{ value: 'a', label: 'A' }]} />
      </>,
    );
    expect(screen.getByLabelText('Name')).toHaveAccessibleDescription('Legal name');
    expect(screen.getByLabelText('Kind')).toBeInTheDocument();
  });

  it('opens a dialog, focuses into it, closes on Escape and restores focus', async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    const opener = screen.getByRole('button', { name: 'Open' });
    await user.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Confirm' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('button', { name: 'Inside' })).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Close dialog' })).toHaveTextContent('Close');

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('focuses the marked control, else the title of a dialog without controls', async () => {
    const user = userEvent.setup();
    const { unmount } = render(
      <DialogHarness>
        <Button>First</Button>
        <Button {...AUTOFOCUS}>Marked</Button>
      </DialogHarness>,
    );
    await user.click(screen.getByRole('button', { name: 'Open' }));
    expect(screen.getByRole('button', { name: 'Marked' })).toHaveFocus();
    unmount();

    render(
      <DialogHarness>
        <p>Nothing to do here.</p>
      </DialogHarness>,
    );
    await user.click(screen.getByRole('button', { name: 'Open' }));
    expect(screen.getByRole('heading', { name: 'Confirm' })).toHaveFocus();
  });

  it('traps Tab inside the dialog', async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    await user.click(screen.getByRole('button', { name: 'Open' }));
    const dialog = screen.getByRole('dialog');
    await user.tab();
    await user.tab();
    await user.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  });

  it('gives tables an accessible name and column scope', () => {
    render(
      <Table caption="People">
        <THead>
          <TR>
            <TH>Name</TH>
          </TR>
        </THead>
        <TBody>
          <TR>
            <TD>Ada</TD>
          </TR>
        </TBody>
      </Table>,
    );
    expect(screen.getByRole('table', { name: 'People' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Name' })).toHaveAttribute('scope', 'col');
  });

  it('switches tabs by click and by Home and End keys', async () => {
    const user = userEvent.setup();
    function Harness() {
      const [value, setValue] = useState('one');
      return (
        <Tabs
          label="Sample"
          value={value}
          onChange={setValue}
          tabs={[
            { id: 'one', label: 'One', content: 'first' },
            { id: 'two', label: 'Two', content: 'second' },
            { id: 'three', label: 'Three', content: 'third' },
          ]}
        />
      );
    }
    render(<Harness />);
    expect(screen.getByRole('tabpanel')).toHaveTextContent('first');
    await user.click(screen.getByRole('tab', { name: 'Two' }));
    expect(screen.getByRole('tabpanel')).toHaveTextContent('second');
    await user.keyboard('{End}');
    expect(screen.getByRole('tabpanel')).toHaveTextContent('third');
    await user.keyboard('{Home}');
    expect(screen.getByRole('tabpanel')).toHaveTextContent('first');
  });

  it('announces toasts and dismisses them', async () => {
    const user = userEvent.setup();
    function Trigger() {
      const toast = useToast();
      return (
        <>
          <Button
            onClick={() => {
              toast.notify('Saved', 'success');
            }}
          >
            Save
          </Button>
          <Button
            onClick={() => {
              toast.notify('Failed', 'error');
            }}
          >
            Fail
          </Button>
        </>
      );
    }
    render(
      <ToastProvider durationMs={60_000}>
        <Trigger />
      </ToastProvider>,
    );
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(screen.getByRole('status')).toHaveTextContent('Saved');
    await user.click(screen.getByRole('button', { name: 'Fail' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Failed');
    await user.click(
      screen.getAllByRole('button', { name: 'Dismiss notification' })[0] as HTMLElement,
    );
    expect(screen.queryByText('Saved')).not.toBeInTheDocument();
  });
});

describe('RouteFallback', () => {
  it('announces that a screen is loading', () => {
    render(<RouteFallback />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading screen...');
  });
});
