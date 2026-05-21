class Tag < ApplicationRecord
  has_many :taggables
  has_many :posts, through: :taggables, source: :taggable, source_type: "Post"

  validates :name, presence: true, uniqueness: true
  validates :slug, presence: true, uniqueness: true
end
