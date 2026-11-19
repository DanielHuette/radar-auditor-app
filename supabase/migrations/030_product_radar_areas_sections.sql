with mapping(letter, section) as (
  values
    ('a', 'google'), ('a', 'bing'), ('a', 'openai'), ('a', 'anthropic'),
    ('b', 'google'), ('b', 'bing'),
    ('c', 'google'),
    ('d', 'google'), ('d', 'bing'),
    ('e', 'google'), ('e', 'bing'),
    ('f', 'google'),
    ('g', 'google'),
    ('h', 'apple'), ('h', 'bing'),
    ('i', 'google'),
    ('j', 'google'), ('j', 'bing'),
    ('k', 'openai'), ('k', 'anthropic'),
    ('m', 'google')
),
converted as (
  select
    product.id,
    coalesce(
      (
        select array_agg(distinct section order by section)
        from (
          select entry as section
          from unnest(product.radar_areas) as entry
          where entry in ('google', 'bing', 'apple', 'openai', 'anthropic')
          union
          select mapping.section
          from unnest(product.radar_areas) as entry
          join mapping on mapping.letter = entry
        ) as sections
      ),
      '{}'::text[]
    ) as radar_areas
  from crm.products as product
  where exists (
    select 1
    from unnest(product.radar_areas) as entry
    where entry in ('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o')
  )
)
update crm.products as product
set radar_areas = converted.radar_areas
from converted
where product.id = converted.id;
