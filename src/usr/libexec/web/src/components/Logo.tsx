// PAI wordmark (owner-supplied, ~/Downloads/pailogo.svg). The source ships as
// white strokes on transparent; we fill the letterforms with currentColor so the
// mark inherits the header ink and adapts to light/dark, and scales crisply at
// any header size. evenodd keeps the counters (the holes in P and A) open.
export function Logo({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 1124 393"
      role="img"
      aria-label="PAI"
      fill="currentColor"
      fillRule="evenodd"
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* The divider bar between the mark and "PAI" is a zero-area stroked line
          in the source; re-add it as a filled rect so it survives filling. */}
      <rect x="345.5" y="1.5" width="3" height="390" />
      <path d="M347.008 1.5L347.008 391.5M59.6956 196.5L2.00797 1.5H61.2238L87.9664 121.471H89.4945L121.204 1.5H167.812L199.521 121.852H201.049L227.792 1.5H287.008L229.32 196.5L287.008 391.5H227.792L201.049 271.529H199.521L167.812 391.5H121.203L89.4944 271.148H87.9662L61.2236 391.5H2.00781L59.6956 196.5ZM178.509 196.5L145.272 87.5742H143.744L110.507 196.5L143.744 305.426H145.272L178.509 196.5ZM1122.18 1.5V391.5H1074.62V1.5H1122.18ZM725.811 391.5H675.953L820.158 1.5H869.249L1013.45 391.5H963.595L846.237 63.1992H843.169L725.811 391.5ZM744.22 239.156H945.186V281.051H744.22V239.156ZM407.008 391.5V1.5H539.707C570.517 1.5 595.701 7.02245 615.261 18.0674C634.948 28.9854 649.522 43.7754 658.982 62.4375C668.443 81.0996 673.173 101.92 673.173 124.898C673.173 147.877 668.443 168.761 658.982 187.55C649.65 206.339 635.204 221.319 615.644 232.491C596.085 243.536 571.028 249.059 540.474 249.059H445.36V207.164H538.94C560.034 207.164 576.972 203.546 589.757 196.31C602.541 189.073 611.809 179.298 617.562 166.983C623.443 154.542 626.383 140.514 626.383 124.898C626.383 109.283 623.443 95.3184 617.562 83.0039C611.809 70.6894 602.477 61.041 589.565 54.0586C576.653 46.9492 559.522 43.3945 538.173 43.3945H454.565V391.5H407.008Z" />
    </svg>
  );
}
