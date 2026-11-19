import { useEffect } from "preact/hooks";
import { LocationProvider, Route, Router, lazy, useLocation } from "preact-iso";
import { Logo } from "./components/Logo";
import { Shell } from "./components/Shell";
import { Szene } from "./components/Szene";
import { usePasswordRecovery } from "./hooks/recovery";
import { AdminContext, useAdminStatus } from "./hooks/role";
import { useSession } from "./hooks/useSession";
import { NotFound } from "./pages/NotFound";
import { Pruefen } from "./pages/Pruefen";
import { SetPassword } from "./pages/SetPassword";
import { SignIn } from "./pages/SignIn";

const SIGN_IN_PATH = "/anmelden";

const Auswertung = lazy(() => import("./pages/Auswertung").then((module) => module.Auswertung));
const Angebot = lazy(() => import("./pages/Angebot").then((module) => module.Angebot));
const Settings = lazy(() => import("./pages/Settings").then((module) => module.Settings));
const Kundensuche = lazy(() => import("./pages/Kundensuche").then((module) => module.Kundensuche));

function ToStart() {
  const { route } = useLocation();
  useEffect(() => {
    route("/", true);
  }, [route]);
  return null;
}

function Splash() {
  return (
    <div class="splash" role="status" aria-label="App wird geladen">
      <Logo size={56} />
      <span class="spinner" aria-hidden="true" />
    </div>
  );
}

function SignedIn({ admin, email }: { admin: boolean | undefined; email: string }) {
  return (
    <AdminContext.Provider value={admin}>
      <Shell admin={admin} email={email}>
        <Router>
          <Route path="/" component={Pruefen} />
          <Route path="/auswertung" component={Auswertung} />
          <Route path="/auswertung/:auditId" component={Auswertung} />
          <Route path="/angebot/:auditId?" component={Angebot} />
          <Route path="/dokumente" component={ToStart} />
          <Route path="/kunden" component={Kundensuche} />
          <Route path="/einstellungen" component={Settings} />
          <Route default component={NotFound} />
        </Router>
      </Shell>
    </AdminContext.Provider>
  );
}

interface GateViewProps {
  session: ReturnType<typeof useSession>;
  recovery: ReturnType<typeof usePasswordRecovery>;
  admin: boolean | undefined;
  onSignIn: boolean;
}

function GateView({ session, recovery, admin, onSignIn }: GateViewProps) {
  if (session === undefined || (session === null && !onSignIn) || (session && onSignIn)) return <Splash />;
  if (session === null) return <SignIn />;
  if (recovery.pending) return <SetPassword onDone={recovery.finish} />;
  return <SignedIn admin={admin} email={session.user.email ?? ""} />;
}

function Gate() {
  const session = useSession();
  const recovery = usePasswordRecovery();
  const admin = useAdminStatus(session?.user.id);
  const { path, route } = useLocation();
  const onSignIn = path === SIGN_IN_PATH;

  useEffect(() => {
    if (session === null && !onSignIn) route(SIGN_IN_PATH, true);
    if (session && onSignIn) route("/", true);
  }, [session, onSignIn, route]);

  return (
    <>
      <Szene leise={session !== null && session !== undefined} />
      <GateView session={session} recovery={recovery} admin={admin} onSignIn={onSignIn} />
    </>
  );
}

export function App() {
  return (
    <LocationProvider>
      <Gate />
    </LocationProvider>
  );
}
