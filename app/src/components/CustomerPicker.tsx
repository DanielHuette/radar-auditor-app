import { listCustomers } from "../api/queries";
import type { Customer } from "../api/types";
import { useIsAdmin } from "../hooks/role";
import { useAsync } from "../hooks/useAsync";
import { hostOf } from "../lib/format";

function CustomerRow({ customer, onPick }: { customer: Customer; onPick: (customer: Customer) => void }) {
  const place = [hostOf(customer.website).replace(/^www\./, ""), customer.city].filter((part) => part !== "").join(" · ");
  return (
    <li>
      <button
        type="button"
        class="request"
        onClick={() => {
          onPick(customer);
        }}
      >
        <span class="request__title">{customer.name}</span>
        {place !== "" && <span class="request__meta">{place}</span>}
      </button>
    </li>
  );
}

function CustomerList({ onPick }: { onPick: (customer: Customer) => void }) {
  const state = useAsync(listCustomers, []);
  if (state.loading) return null;
  if (state.error !== null) {
    return (
      <section class="customers panel stack stack--tight" aria-label="Kunde wählen">
        <p class="field__error" role="alert">
          {state.error}
        </p>
        <button type="button" class="btn btn--small" onClick={state.reload}>
          Noch mal versuchen
        </button>
      </section>
    );
  }
  const customers = state.data ?? [];
  if (customers.length === 0) return null;
  return (
    <section class="customers panel stack stack--tight" aria-labelledby="customers-title">
      <h2 id="customers-title" class="customers__title">
        Kunde wählen
      </h2>
      <ul class="requests__list">
        {customers.map((customer) => (
          <CustomerRow key={customer.id} customer={customer} onPick={onPick} />
        ))}
      </ul>
    </section>
  );
}

export function CustomerPicker({ onPick }: { onPick: (customer: Customer) => void }) {
  return useIsAdmin() ? <CustomerList onPick={onPick} /> : null;
}
